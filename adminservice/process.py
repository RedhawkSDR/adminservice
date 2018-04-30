import os
import sys
import time
import errno
import shlex
import traceback
import signal
import subprocess
from exceptions import Exception

from adminservice.medusa import asyncore_25 as asyncore

from adminservice.states import ProcessStates, RUNNING_STATES
from adminservice.states import AdminServiceStates
from adminservice.states import getProcessStateDescription
from adminservice.states import ALL_STOPPED_STATES
from adminservice.states import STOPPED_STATES

from adminservice.options import decode_wait_status
from adminservice.options import signame
from adminservice.options import ProcessException, BadCommand

from adminservice.dispatchers import EventListenerStates

from adminservice import events

from adminservice.datatypes import integer
from adminservice.datatypes import RestartUnconditionally

from adminservice.socket_manager import SocketManager

class Subprocess:

    """A class to manage a subprocess."""

    # Initial state; overridden by instance variables

    pid = 0 # Subprocess pid; 0 when not running
    config = None # ProcessConfig instance
    state = None # process state code
    listener_state = None # listener state code (if we're an event listener)
    event = None # event currently being processed (if we're an event listener)
    laststart = 0 # Last time the subprocess was started; 0 if never
    laststop = 0  # Last time the subprocess was stopped; 0 if never
    laststopreport = 0 # Last time "waiting for x to stop" logged, to throttle
    delay = 0 # If nonzero, delay starting or killing until this time
    administrative_stop = False # true if process has been stopped by an admin
    system_stop = False # true if process has been stopped by the system
    killing = False # true if we are trying to kill this process
    backoff = 0 # backoff counter (to startretries)
    dispatchers = None # asnycore output dispatchers (keyed by fd)
    pipes = None # map of channel name to file descriptor #
    exitstatus = None # status attached to dead process by finsh()
    spawnerr = None # error message attached by spawn() if any
    group = None # ProcessGroup instance if process is in the group
    waits_left = None # Number of transition waits left until failing transition

    def __init__(self, config):
        """Constructor.

        Argument is a ProcessConfig instance.
        """
        self.config = config
        self.dispatchers = {}
        self.pipes = {}
        self.state = ProcessStates.STOPPED if config.is_enabled() else ProcessStates.DISABLED

    def removelogs(self):
        for dispatcher in self.dispatchers.values():
            if hasattr(dispatcher, 'removelogs'):
                dispatcher.removelogs()

    def reopenlogs(self):
        for dispatcher in self.dispatchers.values():
            if hasattr(dispatcher, 'reopenlogs'):
                dispatcher.reopenlogs()

    def drain(self):
        for dispatcher in self.dispatchers.values():
            # note that we *must* call readable() for every
            # dispatcher, as it may have side effects for a given
            # dispatcher (eg. call handle_listener_state_change for
            # event listener processes)
            if dispatcher.readable():
                dispatcher.handle_read_event()
            if dispatcher.writable():
                dispatcher.handle_write_event()

    def write(self, chars):
        if not self.pid or self.killing:
            raise OSError(errno.EPIPE, "Process already closed")

        stdin_fd = self.pipes['stdin']
        if stdin_fd is None:
            raise OSError(errno.EPIPE, "Process has no stdin channel")

        dispatcher = self.dispatchers[stdin_fd]
        if dispatcher.closed:
            raise OSError(errno.EPIPE, "Process' stdin channel is closed")

        dispatcher.input_buffer += chars
        dispatcher.flush() # this must raise EPIPE if the pipe is closed

    def get_execv_args(self):
        """Internal: turn a program name into a file name, using $PATH,
        make sure it exists / is executable, raising a ProcessException
        if not """
        try:
            cmd = self.config.get_start_command()
            commandargs = shlex.split(cmd)
            self.config.options.logger.debug("Start command: %s" % cmd)
        except ValueError, e:
            raise BadCommand("can't parse command %r: %s" % \
                (self.config.command, str(e)))

        if commandargs:
            program = commandargs[0]
        else:
            raise BadCommand("command is empty")

        if "/" in program:
            filename = program
            try:
                st = self.config.options.stat(filename)
            except OSError:
                st = None

        else:
            path = self.config.options.get_path()
            found = None
            st = None
            for dir in path:
                found = os.path.join(dir, program)
                try:
                    st = self.config.options.stat(found)
                except OSError:
                    pass
                else:
                    break
            if st is None:
                filename = program
            else:
                filename = found

        # check_execv_args will raise a ProcessException if the execv
        # args are bogus, we break it out into a separate options
        # method call here only to service unit tests
        self.config.options.check_execv_args(filename, commandargs, st)

        return filename, commandargs

    event_map = {
        ProcessStates.BACKOFF: events.ProcessStateBackoffEvent,
        ProcessStates.FATAL:   events.ProcessStateFatalEvent,
        ProcessStates.UNKNOWN: events.ProcessStateUnknownEvent,
        ProcessStates.DISABLED: events.ProcessStateDisabledEvent,
        ProcessStates.STOPPED: events.ProcessStateStoppedEvent,
        ProcessStates.EXITED:  events.ProcessStateExitedEvent,
        ProcessStates.RUNNING: events.ProcessStateRunningEvent,
        ProcessStates.STARTING: events.ProcessStateStartingEvent,
        ProcessStates.STOPPING: events.ProcessStateStoppingEvent,
        }

    def change_state(self, new_state, expected=True):
        old_state = self.state
        if new_state is old_state:
            # exists for unit tests
            return False

        event_class = self.event_map.get(new_state)
        if event_class is not None:
            event = event_class(self, old_state, expected)
            events.notify(event)

        if new_state == ProcessStates.BACKOFF:
            now = time.time()
            self.backoff = self.backoff + 1
            self.delay = now + self.backoff

        elif (new_state == ProcessStates.STOPPED or new_state == ProcessStates.EXITED) and not self.config.is_enabled() and old_state != ProcessStates.DISABLED:
            new_state = ProcessStates.DISABLED

        self.state = new_state

    def _assertInState(self, *states):
        if self.state not in states:
            current_state = getProcessStateDescription(self.state)
            allowable_states = ' '.join(map(getProcessStateDescription, states))
            raise AssertionError('Assertion failed for %s: %s not in %s' %  (
                self.config.name, current_state, allowable_states))

    def record_spawnerr(self, msg):
        self.spawnerr = msg
        self.config.options.logger.info("spawnerr: %s" % msg)

    def spawn(self):
        """Start the subprocess.  It must not be running already.

        Return the process id.  If the fork() call fails, return None.
        """
        options = self.config.options

        if self.pid:
            msg = 'process %r already running' % self.config.name
            options.logger.warn(msg)
            return

        self.killing = False
        self.spawnerr = None
        self.exitstatus = None
        self.system_stop = False
        self.administrative_stop = False

        self.laststart = time.time()

        self._assertInState(ProcessStates.EXITED, ProcessStates.FATAL,
                            ProcessStates.BACKOFF, ProcessStates.STOPPED)

        self.change_state(ProcessStates.STARTING)

        try:
            filename, argv = self.get_execv_args()
        except ProcessException, what:
            self.record_spawnerr(what.args[0])
            self._assertInState(ProcessStates.STARTING)
            self.change_state(ProcessStates.BACKOFF)
            return

        try:
            self.dispatchers, self.pipes = self.config.make_dispatchers(self)
        except (OSError, IOError), why:
            code = why.args[0]
            if code == errno.EMFILE:
                # too many file descriptors open
                msg = 'too many open files to spawn %r' % self.config.name
            else:
                msg = 'unknown error making dispatchers for %r: %s' % (
                      self.config.name, errno.errorcode.get(code, code))
            self.record_spawnerr(msg)
            self._assertInState(ProcessStates.STARTING)
            self.change_state(ProcessStates.BACKOFF)
            return

        try:
            pid = options.fork()
        except OSError, why:
            code = why.args[0]
            if code == errno.EAGAIN:
                # process table full
                msg  = ('Too many processes in process table to spawn %r' %
                        self.config.name)
            else:
                msg = 'unknown error during fork for %r: %s' % (
                      self.config.name, errno.errorcode.get(code, code))
            self.record_spawnerr(msg)
            self._assertInState(ProcessStates.STARTING)
            self.change_state(ProcessStates.BACKOFF)
            options.close_parent_pipes(self.pipes)
            options.close_child_pipes(self.pipes)
            return

        if pid != 0:
            return self._spawn_as_parent(pid)

        else:
            return self._spawn_as_child(filename, argv)

    def _spawn_as_parent(self, pid):
        # Parent
        self.pid = pid
        options = self.config.options
        options.close_child_pipes(self.pipes)
        options.logger.info('spawned: %r with pid %s' % (self.config.name, pid))
        self.spawnerr = None
        self.delay = time.time() + self.config.startsecs
        options.pidhistory[pid] = self
        return pid

    def _prepare_child_fds(self):
        options = self.config.options
        options.dup2(self.pipes['child_stdin'], 0)
        options.dup2(self.pipes['child_stdout'], 1)
        if self.config.redirect_stderr:
            options.dup2(self.pipes['child_stdout'], 2)
        else:
            options.dup2(self.pipes['child_stderr'], 2)
        for i in range(3, options.minfds):
            options.close_fd(i)

    def handle_term(self, sig, frame):
        self.config.options.mood = AdminServiceStates.SHUTDOWN

    def _spawn_as_child(self, filename, argv):
        options = self.config.options
        exit_code = 127

        # Add a handler to let the detached processes know to stop statusing
        signal.signal(signal.SIGTERM, self.handle_term)

        try:
            # prevent child from receiving signals sent to the
            # parent by calling os.setpgrp to create a new process
            # group for the child; this prevents, for instance,
            # the case of child processes being sent a SIGINT when
            # running adminservice in foreground mode and Ctrl-C in
            # the terminal window running adminserviced is pressed.
            # Presumably it also prevents HUP, etc received by
            # adminserviced from being sent to children.
            options.setpgrp()

            self._prepare_child_fds()
            # sending to fd 2 will put this output in the stderr log

            # set user
            setuid_msg = self.set_uid()
            if setuid_msg:
                uid = self.config.uid
                msg = "couldn't setuid to %s: %s\n" % (uid, setuid_msg)
                options.write(2, "adminservice: " + msg)
                return # finally clause will exit the child process

            # set environment
            env = os.environ.copy()
            env['ADMINSERVICE_ENABLED'] = '1'
            serverurl = self.config.serverurl
            if serverurl is None: # unset
                serverurl = options.serverurl # might still be None
            if serverurl:
                env['ADMINSERVICE_SERVER_URL'] = serverurl
            env['ADMINSERVICE_PROCESS_NAME'] = self.config.name
            if self.group:
                env['ADMINSERVICE_GROUP_NAME'] = self.group.config.name
            if self.config.environment is not None:
                env.update(self.config.environment)

            # change directory
            try:
                cwd = self.config.directory
                if cwd is not None:
                    options.chdir(cwd)
            except OSError, why:
                code = errno.errorcode.get(why.args[0], why.args[0])
                msg = "couldn't chdir to %s: %s\n" % (cwd, code)
                options.write(2, "adminservice: " + msg)
                return # finally clause will exit the child process

            # set umask, then execve
            try:
                if self.config.umask is not None:
                    options.setumask(self.config.umask)
                self.config.alive = True
                if self.config.run_detached:
                    # If the process is already running, we don't need to start it again.
                    if not self.config.check_status():

                        # Remove the pid file, use its existence to check if the process has started
                        if os.path.exists(self.config.pid_file):
                            os.remove(self.config.pid_file)

                        # Start the process as a daemon
                        daemonize(self.config.directory, self.config.umask, argv, env)

                        # Give the process some time to start up
                        num_checks = 0
                        while not os.path.exists(self.config.pid_file) and (num_checks < 10):
                            time.sleep(1)
                            num_checks += 1

                        # If there's a script to check if the config was started, run it now.
                        # The assumption is it will return when it can determine if the process
                        # is in either a good or bad state.
                        if self.config.started_status_script is not None and not self.config.is_started():
                            raise "Bad start status for %s" % self.config.name

                    else:
                        self.change_state(ProcessStates.RUNNING)
                        msg = "Process for %s was already running. Not going to relaunch\n" % self.config.name
                        options.write(1, "adminservice: " + msg)

                    # Check status every second
                    status = self.config.check_status()
                    while status and self.config.options.mood == AdminServiceStates.RUNNING:
                        exit_code = 0
                        time.sleep(1)
                        status = self.config.check_status()
                    
                    # This is one of the few times that we can remove the pid file.
                    #    Either the service is shutting down - leave the pid file
                    #    The process ended/died - remove the pid file
                    if not status and os.path.exists(self.config.pid_file):
                        os.remove(self.config.pid_file)
                else:
                    # This call won't return
                    options.execve(argv[0], argv, env)
            except SystemExit, e:
                # The fork called sys.exit, things are ok
                exit_code = e.code
            except OSError, why:
                code = errno.errorcode.get(why.args[0], why.args[0])
                self.config.alive = False
                msg = "adminservice: couldn't exec %s: %s\n" % (argv[0], code)
                options.write(2, msg)
            except:
                (fil, fun, line), t,v,tbinfo = asyncore.compact_traceback()
                self.config.alive = False
                error = '%s, %s: file: %s line: %s' % (t, v, fil, line)
                msg = "adminservice: couldn't exec %s: %s\n" % (filename, error)
                options.write(2, msg)

            # this point should only be reached if execve failed or the spawnve'd process is finished.
            # the finally clause will exit the child process.
        except Exception, e:
            options.write(2, "Exception spawning process: %s" % e)
        finally:
            if exit_code != 0:
                options.write(2, "adminservice: child process was not spawned (%s)\n" % exit_code)
            options._exit(exit_code) # exit process with code for spawn failure
            

    def stop(self):
        """ Administrative stop """
        self.administrative_stop = True
        self.laststopreport = 0

        # We want to leave the processes up if they have pid_files and
        # admin service is shutting down, otherwise run the stop scripts
        # 
        shutting_down = (self.config.options.mood < AdminServiceStates.RUNNING)
        if not shutting_down or not self.config.run_detached:
            self.run_script(self.config.stop_pre_script)
                    
        killval = self.kill(self.config.stopsignal, shutting_down)

        if not shutting_down or not self.config.run_detached:
            self.config.alive = False
            self.run_script(self.config.stop_post_script)

        return killval

    def run_script(self, script_to_run):
        if script_to_run is not None:
            pre_files = [script_to_run]
            if os.path.isdir(script_to_run):
                pre_files = os.listdir(script_to_run)
            try:
                for script in pre_files:
                    if os.path.exists(script):
                        subprocess.call(["/bin/bash", script])
            except OSError, e:
                self.config.options.write(2, "adminservice: unable to execute script(s) %s: %s\n" % (pre_files, repr(e)))
            except:
                (fil, fun, line), t,v,tbinfo = asyncore.compact_traceback()
                error = '%s, %s: file: %s line: %s' % (t, v, fil, line)
                msg = "adminservice: couldn't run script at %s: %s\n" % (script_to_run, error)
                self.config.options.write(2, msg)

    def stop_report(self):
        """ Log a 'waiting for x to stop' message with throttling. """
        if self.state == ProcessStates.STOPPING:
            now = time.time()
            if now > (self.laststopreport + 2): # every 2 seconds
                self.config.options.logger.info(
                    'waiting for %s to stop' % self.config.name)
                self.laststopreport = now

    def give_up(self):
        self.delay = 0
        self.backoff = 0
        self.system_stop = True
        self._assertInState(ProcessStates.BACKOFF)
        self.change_state(ProcessStates.FATAL)

    def kill(self, sig, shutdown=False):
        """Send a signal to the subprocess.  This may or may not kill it.

        Return None if the signal was sent, or an error message string
        if an error occurred or if the subprocess is not running.
        """
        now = time.time()
        options = self.config.options
        detached_pid = None
        stop_command = None
        
        if self.config.run_detached and os.path.exists(self.config.pid_file):
            try:
                detached_pid = integer(self.config.get_pid())
                options.logger.debug("Found detached pid %s for native pid %s" % (detached_pid, self.pid))
            except Exception, e:
                stop_command = self.config.get_stop_command()
                if stop_command is not None:
                    options.logger.debug("Pid file isn't an integer, using stop command instead %s for %s" % (stop_command, self.pid))
                else:
                    raise e

        # If the process is in BACKOFF and we want to stop or kill it, then
        # BACKOFF -> STOPPED.  This is needed because if startretries is a
        # large number and the process isn't starting successfully, the stop
        # request would be blocked for a long time waiting for the retries.
        if self.state == ProcessStates.BACKOFF:
            msg = "Attempted to kill %s, which is in BACKOFF state." % self.config.name
            options.logger.debug(msg)
            self.change_state(ProcessStates.STOPPED)
            return None

        if not self.pid:
            msg = "Attempted to kill %s with sig %s but it wasn't running" % (self.config.name, signame(sig))
            options.logger.debug(msg)
            return msg

        # If we're in the stopping state, then we've already sent the stop
        # signal and this is the kill signal
        if self.state == ProcessStates.STOPPING:
            killasgroup = self.config.killasgroup
        else:
            killasgroup = self.config.stopasgroup

        as_group = ""
        if killasgroup:
            as_group = "process group "

        options.logger.debug('Killing %s (pid %s) %swith signal %s'
                             % (self.config.name,
                                self.pid,
                                as_group,
                                signame(sig))
                             )

        # RUNNING/STARTING/STOPPING -> STOPPING
        self.killing = True
        self.delay = now + self.config.stopwaitsecs
        # we will already be in the STOPPING state if we're doing a
        # SIGKILL as a result of overrunning stopwaitsecs
        self._assertInState(ProcessStates.RUNNING,
                            ProcessStates.STARTING,
                            ProcessStates.STOPPING)
        self.change_state(ProcessStates.STOPPING)

        pid = self.pid
        if killasgroup:
            # send to the whole process group instead
            pid = -self.pid

        try:
            if detached_pid is not None:
                if shutdown:
                    options.logger.info("Leaving %s(%d) running, system is shutting down" % (self.config.name, detached_pid))
                else:
                    try:
                        options.kill(detached_pid, sig)
                        options.logger.warn("Killed detached pid %d" % detached_pid)
                        if os.path.exists(self.config.pid_file) and not self.config.check_status():
                            options.logger.trace("Removing %s after killing detached process" % self.config.pid_file)
                            os.remove(self.config.pid_file)
                    except Exception, e:
                        options.logger.warn("Problem killing detached pid %d: %s" % (detached_pid, e))
            if stop_command is not None:
                if shutdown:
                    options.logger.info("Leaving %s(%d) running, system is shutting down" % (self.config.name, pid))
                else:
                    try:
                        val = os.system(stop_command)
                        options.logger.debug("Stop command for %s had return code of %s" % (pid, val))
                        if os.path.exists(self.config.pid_file) and not self.config.check_status():
                            options.logger.trace("Removing %s after killing detached process" % self.config.pid_file)
                            os.remove(self.config.pid_file)
                    except Exception, e:
                        options.logger.warn("Problem running stop command for %s: %s" % (pid, e))
            options.logger.info("Sending %s to pid %s" % (sig, pid))
            options.kill(pid, sig)
        except:
            tb = traceback.format_exc()
            msg = 'unknown problem killing %s (%s):%s' % (self.config.name,
                                                          self.pid, tb)
            options.logger.critical(msg)
            self.change_state(ProcessStates.UNKNOWN)
            self.pid = 0
            self.killing = False
            self.delay = 0
            return msg

        return None

    def signal(self, sig):
        """Send a signal to the subprocess, without intending to kill it.

        Return None if the signal was sent, or an error message string
        if an error occurred or if the subprocess is not running.
        """
        options = self.config.options
        if not self.pid:
            msg = ("attempted to send %s sig %s but it wasn't running" %
                   (self.config.name, signame(sig)))
            options.logger.debug(msg)
            return msg

        options.logger.debug('sending %s (pid %s) sig %s'
                             % (self.config.name,
                                self.pid,
                                signame(sig))
                             )

        self._assertInState(ProcessStates.RUNNING,
                            ProcessStates.STARTING,
                            ProcessStates.STOPPING)

        try:
            options.kill(self.pid, sig)
        except:
            tb = traceback.format_exc()
            msg = 'unknown problem sending sig %s (%s):%s' % (
                                self.config.name, self.pid, tb)
            options.logger.critical(msg)
            self.change_state(ProcessStates.UNKNOWN)
            self.pid = 0
            return msg

        return None

    def finish(self, pid, sts):
        """ The process was reaped and we need to report and manage its state
        """
        self.drain()

        es, msg = decode_wait_status(sts)

        now = time.time()
        self.laststop = now
        processname = self.config.name

        if now > self.laststart:
            too_quickly = now - self.laststart < self.config.startsecs
        else:
            too_quickly = False
            self.config.options.logger.warn(
                "process %r (%s) laststart time is in the future, don't "
                "know how long process was running so assuming it did "
                "not exit too quickly" % (self.config.name, self.pid))

        exit_expected = es in self.config.exitcodes

        if self.killing:
            # likely the result of a stop request
            # implies STOPPING -> STOPPED
            self.killing = False
            self.delay = 0
            self.exitstatus = es

            msg = "stopped: %s (%s)" % (processname, msg)
            self._assertInState(ProcessStates.STOPPING)
            self.change_state(ProcessStates.STOPPED)

        elif too_quickly:
            # the program did not stay up long enough to make it to RUNNING
            # implies STARTING -> BACKOFF
            self.exitstatus = None
            self.spawnerr = 'Exited too quickly (process log may have details)'
            msg = "exited: %s (%s)" % (processname, msg + "; not expected")
            self._assertInState(ProcessStates.STARTING)
            self.change_state(ProcessStates.BACKOFF)

        else:
            # this finish was not the result of a stop request, the
            # program was in the RUNNING state but exited
            # implies RUNNING -> EXITED normally but see next comment
            self.delay = 0
            self.backoff = 0
            self.exitstatus = es

            # if the process was STARTING but a system time change causes
            # self.laststart to be in the future, the normal STARTING->RUNNING
            # transition can be subverted so we perform the transition here.
            if self.state == ProcessStates.STARTING:
                self.change_state(ProcessStates.RUNNING)

            self._assertInState(ProcessStates.RUNNING)

            if exit_expected:
                # expected exit code
                msg = "exited: %s (%s)" % (processname, msg + "; expected")
                self.change_state(ProcessStates.EXITED, expected=True)
            else:
                # unexpected exit code
                self.spawnerr = 'Bad exit code %s' % es
                msg = "exited: %s (%s)" % (processname, msg + "; not expected")
                self.change_state(ProcessStates.EXITED, expected=False)

        self.config.options.logger.info(msg)

        # Clean up the pid file, if present, since we're done with the process at this point
        if self.config.options.mood == AdminServiceStates.RUNNING and self.config.run_detached and os.path.exists(self.config.pid_file):
            self.config.options.logger.trace("Finishing process, removing pid file: %s" % self.config.pid_file)
            os.remove(self.config.pid_file)

        self.pid = 0
        self.config.options.close_parent_pipes(self.pipes)
        self.pipes = {}
        self.dispatchers = {}

        # if we died before we processed the current event (only happens
        # if we're an event listener), notify the event system that this
        # event was rejected so it can be processed again.
        if self.event is not None:
            # Note: this should only be true if we were in the BUSY
            # state when finish() was called.
            events.notify(events.EventRejectedEvent(self, self.event))
            self.event = None

    def set_uid(self):
        if self.config.uid is None:
            return
        msg = self.config.options.drop_privileges(self.config.uid, self.config.gid)
        return msg

    def __cmp__(self, other):
        # sort by priority
        return cmp(self.config.priority, other.config.priority)

    def __repr__(self):
        return '<Subprocess at %s with name %s in state %s>' % (
            id(self),
            self.config.name,
            getProcessStateDescription(self.get_state()))

    def get_state(self):
        return self.state

    def transition(self):
        now = time.time()
        state = self.state

        logger = self.config.options.logger

        if self.config.options.mood > AdminServiceStates.RESTARTING:
            # dont start any processes if adminservice is shutting down
            if state == ProcessStates.EXITED:
                if self.config.is_enabled() and self.config.autorestart:
                    if self.config.autorestart is RestartUnconditionally:
                        # EXITED -> STARTING
                        self.spawn()
                    else: # autorestart is RestartWhenExitUnexpected
                        if self.exitstatus not in self.config.exitcodes:
                            # EXITED -> STARTING
                            self.spawn()
            elif state == ProcessStates.STOPPED and not self.laststart:
                # Remove the pid file, use its existence to check if the process has started
                if self.config.run_detached and os.path.exists(self.config.pid_file) and not self.config.check_status():
                    logger.info("Didn't expect to find the pid file %s, removing now" % self.config.pid_file)
                    os.remove(self.config.pid_file)

                if self.config.is_enabled() and self.config.autostart:
                    # STOPPED -> STARTING
                    self.spawn()
                elif self.config.run_detached:
                    if os.path.exists(self.config.pid_file) and self.config.check_status():
                        # If the process has a pid file and is already running, transition to STARTING
                        # This will handle not starting another process, just need the status thread running
                        logger.info('%s appears to be running already, changing from %s to STARTING state'
                                     % (self.config.name, getProcessStateDescription(self.state)))
                        self.spawn()
            elif state == ProcessStates.DISABLED and os.path.exists(self.config.pid_file):
                # If the process is in the disabled state, it may still be running. Check based on the pid_file config value.
                stat = self.config.check_status()
                if not stat:
                    # If the process isn't running but has a pid file, remove it so we don't keep checking
                    logger.info("Didn't expect to find the pid file %s, removing now" % self.config.pid_file)
                    os.remove(self.config.pid_file)
                else:
                    # If the process has a pid file and is already running, transition to STARTING
                    # This will handle not starting another process, just need the status thread running
                    logger.info('%s appears to be running already, changing it to a startable state' % self.config.name)
                    self.change_state(ProcessStates.STOPPED)
                    self.spawn()
            elif state == ProcessStates.BACKOFF:
                if self.config.is_enabled() and self.backoff <= self.config.startretries:
                    if now > self.delay:
                        # BACKOFF -> STARTING
                        self.spawn()

        if state == ProcessStates.STARTING:
            if now - self.laststart > self.config.startsecs:
                # STARTING -> RUNNING if the proc has started
                # successfully and it has stayed up for at least
                # proc.config.startsecs,
                self.delay = 0
                self.backoff = 0
                self._assertInState(ProcessStates.STARTING)
                self.change_state(ProcessStates.RUNNING)
                msg = (
                    'entered RUNNING state, process has stayed up for '
                    '> than %s seconds (startsecs)' % self.config.startsecs)
                logger.info('success: %s %s' % (self.config.name, msg))

        if state == ProcessStates.BACKOFF:
            if self.backoff > self.config.startretries:
                # BACKOFF -> FATAL if the proc has exceeded its number
                # of retries
                self.give_up()
                msg = ('entered FATAL state, too many start retries too '
                       'quickly')
                logger.info('gave up: %s %s' % (self.config.name, msg))

        elif state == ProcessStates.STOPPING:
            time_left = self.delay - now
            if time_left <= 0:
                # kill processes which are taking too long to stop with a final
                # sigkill.  if this doesn't kill it, the process will be stuck
                # in the STOPPING state forever.
                logger.warn('killing %r (%s) with SIGKILL' % (self.config.name, self.pid))
                shutting_down = (self.config.options.mood < AdminServiceStates.RUNNING)
                self.kill(signal.SIGKILL, shutting_down)

class FastCGISubprocess(Subprocess):
    """Extends Subprocess class to handle FastCGI subprocesses"""

    def __init__(self, config):
        Subprocess.__init__(self, config)
        self.fcgi_sock = None

    def before_spawn(self):
        """
        The FastCGI socket needs to be created by the parent before we fork
        """
        if self.group is None:
            raise NotImplementedError('No group set for FastCGISubprocess')
        if not hasattr(self.group, 'socket_manager'):
            raise NotImplementedError('No SocketManager set for '
                                      '%s:%s' % (self.group, dir(self.group)))
        self.fcgi_sock = self.group.socket_manager.get_socket()

    def spawn(self):
        """
        Overrides Subprocess.spawn() so we can hook in before it happens
        """
        self.before_spawn()
        pid = Subprocess.spawn(self)
        if pid is None:
            #Remove object reference to decrement the reference count on error
            self.fcgi_sock = None
        return pid

    def after_finish(self):
        """
        Releases reference to FastCGI socket when process is reaped
        """
        #Remove object reference to decrement the reference count
        self.fcgi_sock = None

    def finish(self, pid, sts):
        """
        Overrides Subprocess.finish() so we can hook in after it happens
        """
        retval = Subprocess.finish(self, pid, sts)
        self.after_finish()
        return retval

    def _prepare_child_fds(self):
        """
        Overrides Subprocess._prepare_child_fds()
        The FastCGI socket needs to be set to file descriptor 0 in the child
        """
        sock_fd = self.fcgi_sock.fileno()

        options = self.config.options
        options.dup2(sock_fd, 0)
        options.dup2(self.pipes['child_stdout'], 1)
        if self.config.redirect_stderr:
            options.dup2(self.pipes['child_stdout'], 2)
        else:
            options.dup2(self.pipes['child_stderr'], 2)
        for i in range(3, options.minfds):
            options.close_fd(i)

class ProcessGroupBase:
    def __init__(self, config):
        self.config = config
        self.processes = {}
        for pconfig in self.config.process_configs:
            self.processes[pconfig.name] = pconfig.make_process(self)


    def __cmp__(self, other):
        return cmp(self.config.priority, other.config.priority)

    def __repr__(self):
        return '<%s instance at %s named %s>' % (self.__class__, id(self),
                                                 self.config.name)

    def removelogs(self):
        for process in self.processes.values():
            process.removelogs()

    def reopenlogs(self):
        for process in self.processes.values():
            process.reopenlogs()

    def stop_all(self):
        processes = self.processes.values()
        processes.sort()
        processes.reverse() # stop in desc priority order

        for proc in processes:
            state = proc.get_state()
            if state == ProcessStates.RUNNING:
                # RUNNING -> STOPPING
                proc.stop()
            elif state == ProcessStates.STARTING:
                # STARTING -> STOPPING
                proc.stop()
            elif state == ProcessStates.BACKOFF:
                # BACKOFF -> FATAL
                proc.give_up()

    def get_unstopped_processes(self):
        """ Processes which aren't in a state that is considered 'stopped' """
        return [ x for x in self.processes.values() if x.get_state() not in
                 ALL_STOPPED_STATES ]

    def get_dispatchers(self):
        dispatchers = {}
        for process in self.processes.values():
            dispatchers.update(process.dispatchers)
        return dispatchers

    def before_remove(self):
        pass

class ProcessGroup(ProcessGroupBase):
    def transition(self):
        procs = self.processes.values()[:]
        procs.sort()
        last_state = None

        for proc in procs:
            logger = proc.config.options.logger

            # If we're not the first process in the group, check if we need to wait for the previous ones to start
            if last_state is not None and proc.get_state() in STOPPED_STATES:
                previous_state = last_state[0]

                if previous_state in RUNNING_STATES and proc.waits_left is None:
                    proc.waits_left = proc.config.waitforprevious

                # If this process isn't configured to wait, don't bother with these transitions
                if proc.waits_left is not None and previous_state != ProcessStates.RUNNING:
                    # Need to wait if we have a wait time set, the previous process is in the STARTING state and
                    # this process is in the STOPPED state
                    if proc.waits_left > 0 and previous_state == ProcessStates.STARTING:
                        logger.trace("%s startup wait conditions true, returning"
                                     % proc.config.name)
                        proc.waits_left -= 1
                        return
                    # If there are no waits left, or the previous process has transitioned to a new state(not running - a bad state)
                    # and we haven't started, change the process to the FATAL state and skip this process. Otherwise transition
                    elif proc.waits_left == 0 or previous_state != ProcessStates.STARTING:
                        logger.debug("waits = 0 or previous is not starting")
                        if proc.config.failafterwait and proc.state != ProcessStates.FATAL:
                            logger.debug("Marking %s as FATAL, waits left: %d  previous state: %s"
                                         % (proc.config.name, proc.waits_left, getProcessStateDescription(previous_state)))
                            proc.change_state(ProcessStates.FATAL)
                            continue

            # If we're not the first process and we're disabled, we still need to spawn a status thread if its running.
            # Check that the previous state is STARTING, if it is, wait for that to finish before we try to status this process
            elif last_state is not None and last_state[0] == ProcessStates.STARTING and proc.get_state() == ProcessStates.DISABLED:
                logger.trace("%s is disabled, previous process is starting. Waiting for previous to transition"
                             % proc.config.name)
                return

            proc.waits_left = None
            proc.transition()
            if proc.get_state() != ProcessStates.DISABLED:
                last_state = (proc.get_state(), proc)

class FastCGIProcessGroup(ProcessGroup):
    def __init__(self, config, **kwargs):
        ProcessGroup.__init__(self, config)
        sockManagerKlass = kwargs.get('socketManager', SocketManager)
        self.socket_manager = sockManagerKlass(config.socket_config,
                                               logger=config.options.logger)
        # It's not required to call get_socket() here but we want
        # to fail early during start up if there is a config error
        try:
            self.socket_manager.get_socket()
        except Exception, e:
            raise ValueError('Could not create FastCGI socket %s: %s' % (self.socket_manager.config(), e))

class EventListenerPool(ProcessGroupBase):
    def __init__(self, config):
        ProcessGroupBase.__init__(self, config)
        self.event_buffer = []
        self.serial = -1
        self.last_dispatch = 0
        self.dispatch_throttle = 0 # in seconds: .00195 is an interesting one
        self._subscribe()

    def handle_rejected(self, event):
        process = event.process
        procs = self.processes.values()
        if process in procs: # this is one of our processes
            # rebuffer the event
            self._acceptEvent(event.event, head=True)

    def transition(self):
        processes = self.processes.values()
        dispatch_capable = False
        for process in processes:
            process.transition()
            # this is redundant, we do it in _dispatchEvent too, but we
            # want to reduce function call overhead
            if process.state == ProcessStates.RUNNING:
                if process.listener_state == EventListenerStates.READY:
                    dispatch_capable = True
        if dispatch_capable:
            if self.dispatch_throttle:
                now = time.time()
                if now - self.last_dispatch < self.dispatch_throttle:
                    return
            self.dispatch()

    def before_remove(self):
        self._unsubscribe()

    def dispatch(self):
        while self.event_buffer:
            # dispatch the oldest event
            event = self.event_buffer.pop(0)
            ok = self._dispatchEvent(event)
            if not ok:
                # if we can't dispatch an event, rebuffer it and stop trying
                # to process any further events in the buffer
                self._acceptEvent(event, head=True)
                break
        self.last_dispatch = time.time()

    def _acceptEvent(self, event, head=False):
        # events are required to be instances
        # this has a side effect to fail with an attribute error on 'old style' classes
        if not hasattr(event, 'serial'):
            event.serial = new_serial(GlobalSerial)
        if not hasattr(event, 'pool_serials'):
            event.pool_serials = {}
        if not event.pool_serials.has_key(self.config.name):
            event.pool_serials[self.config.name] = new_serial(self)
        else:
            self.config.options.logger.debug(
                'rebuffering event %s for pool %s (buf size=%d, max=%d)' % (
                (event.serial, self.config.name, len(self.event_buffer),
                self.config.buffer_size)))

        if len(self.event_buffer) >= self.config.buffer_size:
            if self.event_buffer:
                # discard the oldest event
                discarded_event = self.event_buffer.pop(0)
                self.config.options.logger.error(
                    'pool %s event buffer overflowed, discarding event %s' % (
                    (self.config.name, discarded_event.serial)))
        if head:
            self.event_buffer.insert(0, event)
        else:
            self.event_buffer.append(event)

    def _dispatchEvent(self, event):
        pool_serial = event.pool_serials[self.config.name]

        for process in self.processes.values():
            if process.state != ProcessStates.RUNNING:
                continue
            if process.listener_state == EventListenerStates.READY:
                payload = str(event)
                try:
                    event_type = event.__class__
                    serial = event.serial
                    envelope = self._eventEnvelope(event_type, serial,
                                                   pool_serial, payload)
                    process.write(envelope)
                except OSError, why:
                    if why.args[0] != errno.EPIPE:
                        raise
                    continue

                process.listener_state = EventListenerStates.BUSY
                process.event = event
                self.config.options.logger.debug(
                    'event %s sent to listener %s' % (
                    event.serial, process.config.name))
                return True

        return False

    def _eventEnvelope(self, event_type, serial, pool_serial, payload):
        event_name = events.getEventNameByType(event_type)
        payload_len = len(payload)
        D = {
            'ver':'3.0',
            'sid':self.config.options.identifier,
            'serial':serial,
            'pool_name':self.config.name,
            'pool_serial':pool_serial,
            'event_name':event_name,
            'len':payload_len,
            'payload':payload,
             }
        return ('ver:%(ver)s server:%(sid)s serial:%(serial)s '
                'pool:%(pool_name)s poolserial:%(pool_serial)s '
                'eventname:%(event_name)s len:%(len)s\n%(payload)s' % D)

    def _subscribe(self):
        for event_type in self.config.pool_events:
            events.subscribe(event_type, self._acceptEvent)
        events.subscribe(events.EventRejectedEvent, self.handle_rejected)

    def _unsubscribe(self):
        for event_type in self.config.pool_events:
            events.unsubscribe(event_type, self._acceptEvent)
        events.unsubscribe(events.EventRejectedEvent, self.handle_rejected)


class GlobalSerial(object):
    def __init__(self):
        self.serial = -1

GlobalSerial = GlobalSerial() # singleton

def new_serial(inst):
    if inst.serial == sys.maxint:
        inst.serial = -1
    inst.serial += 1
    return inst.serial

def daemonize(directory, umask, argv, env, stdin='/dev/null', stdout='/dev/null', stderr='/dev/null'):
    stdin = os.path.abspath(stdin)
    stdout = os.path.abspath(stdout)
    stderr = os.path.abspath(stderr)
    
    # Do first fork
    try: 
        pid = os.fork() 
    except OSError as e:
        sys.stderr.write("fork #1 failed: (%d) %s\n"%(e.errno, e.strerror))
        sys.exit(1)
    if pid > 0:
        return 0 # exit parent

    # Do second fork.
    try: 
        pid = os.fork() 
    except OSError as e: 
        sys.stderr.write("fork #2 failed: (%d) %s\n"%(e.errno, e.strerror))
        sys.exit(1)
    if pid > 0:
        sys.exit(0) # exit parent


    # Decouple from parent environment.
    os.chdir(directory) 
    os.umask(integer(umask) if umask is not None else 0) 
    os.setsid() 

    # Do third fork.
    try: 
        pid = os.fork() 
    except OSError as e: 
        sys.stderr.write("fork #3 failed: (%d) %s\n"%(e.errno, e.strerror))
        sys.exit(1)
    if pid > 0:
        sys.exit(0) # exit parent

    # Now I am a daemon!
    subprocess.Popen(argv, env=env, close_fds=True)
    os._exit(0)
