import os
import time
import datetime
import errno
import types

from adminservice.datatypes import boolean
from adminservice.datatypes import signal_number

from adminservice.options import readFile
from adminservice.options import tailFile
from adminservice.options import NotExecutable
from adminservice.options import NotFound
from adminservice.options import NoPermission
from adminservice.options import make_namespec
from adminservice.options import split_namespec
from adminservice.options import VERSION

from adminservice.events import notify
from adminservice.events import RemoteCommunicationEvent

from adminservice.http import NOT_DONE_YET
from adminservice.xmlrpc import (
    capped_int,
    Faults,
    RPCError,
    )

from adminservice.states import AdminServiceStates
from adminservice.states import getAdminServiceStateDescription
from adminservice.states import ProcessStates
from adminservice.states import getProcessStateDescription
from adminservice.states import (
    ALL_STOPPED_STATES,
    RUNNING_STATES,
    STOPPED_STATES,
    )

API_VERSION  = '3.0'

class AdminServiceNamespaceRPCInterface:
    def __init__(self, adminserviced):
        self.adminserviced = adminserviced

    def _update(self, text):
        self.update_text = text # for unit tests, mainly
        if ( isinstance(self.adminserviced.options.mood, int) and
             self.adminserviced.options.mood < AdminServiceStates.RUNNING ):
            raise RPCError(Faults.SHUTDOWN_STATE)

    # RPC API methods

    def getAPIVersion(self):
        """ Return the version of the RPC API used by adminserviced

        @return string version version id
        """
        self._update('getAPIVersion')
        return API_VERSION

    getVersion = getAPIVersion # b/w compatibility with releases before 3.0

    def getAdminServiceVersion(self):
        """ Return the version of the adminservice package in use by adminserviced

        @return string version version id
        """
        self._update('getAdminServiceVersion')
        return VERSION

    def getIdentification(self):
        """ Return identifying string of adminserviced

        @return string identifier identifying string
        """
        self._update('getIdentification')
        return self.adminserviced.options.identifier

    def getState(self):
        """ Return current state of adminserviced as a struct

        @return struct A struct with keys int statecode, string statename
        """
        self._update('getState')

        state = self.adminserviced.options.mood
        statename = getAdminServiceStateDescription(state)
        data =  {
            'statecode':state,
            'statename':statename,
            }
        return data

    def getPID(self):
        """ Return the PID of adminserviced

        @return int PID
        """
        self._update('getPID')
        return self.adminserviced.options.get_pid()

    def readLog(self, offset, length):
        """ Read length bytes from the main log starting at offset

        @param int offset         offset to start reading from.
        @param int length         number of bytes to read from the log.
        @return string result     Bytes of log
        """
        self._update('readLog')

        logfile = self.adminserviced.options.logfile

        if logfile is None or not os.path.exists(logfile):
            raise RPCError(Faults.NO_FILE, logfile)

        try:
            return readFile(logfile, int(offset), int(length))
        except ValueError, inst:
            why = inst.args[0]
            raise RPCError(getattr(Faults, why))

    readMainLog = readLog # b/w compatibility with releases before 2.1

    def clearLog(self):
        """ Clear the main log.

        @return boolean result always returns True unless error
        """
        self._update('clearLog')

        logfile = self.adminserviced.options.logfile
        if logfile is None or not self.adminserviced.options.exists(logfile):
            raise RPCError(Faults.NO_FILE)

        # there is a race condition here, but ignore it.
        try:
            self.adminserviced.options.remove(logfile)
        except (OSError, IOError):
            raise RPCError(Faults.FAILED)

        for handler in self.adminserviced.options.logger.handlers:
            if hasattr(handler, 'reopen'):
                self.adminserviced.options.logger.info('reopening log file')
                handler.reopen()
        return True

    def shutdown(self):
        """ Shut down the adminservice process

        @return boolean result always returns True unless error
        """
        self._update('shutdown')
        self.adminserviced.options.mood = AdminServiceStates.SHUTDOWN
        self.adminserviced.options.logger.debug("Server mood is now %d" % self.adminserviced.options.mood)
        return True

    def restart(self):
        """ Restart the adminservice process

        @return boolean result  always return True unless error
        """
        self._update('restart')

        self.adminserviced.options.mood = AdminServiceStates.RESTARTING
        return True

    def reloadConfig(self):
        """
        Reload the configuration.

        The result contains three arrays containing names of process
        groups:

        * `added` gives the process groups that have been added
        * `changed` gives the process groups whose contents have
          changed
        * `removed` gives the process groups that are no longer
          in the configuration

        @return array result  [[added, changed, removed]]

        """
        self._update('reloadConfig')
        try:
            self.adminserviced.options.process_config(do_usage=False)
        except ValueError, msg:
            raise RPCError(Faults.CANT_REREAD, msg)

        added, changed, removed = self.adminserviced.diff_to_active()

        added = [group.name for group in added]
        changed = [group.name for group in changed]
        removed = [group.name for group in removed]
        return [[added, changed, removed]] # cannot return len > 1, apparently

    def addProcessGroup(self, name):
        """ Update the config for a running process from config file.

        @param string name         name of process group to add
        @return boolean result     true if successful
        """
        self._update('addProcessGroup')

        for config in self.adminserviced.options.process_group_configs:
            if config.name == name:
                result = self.adminserviced.add_process_group(config)
                if not result:
                    raise RPCError(Faults.ALREADY_ADDED, name)
                return True
        raise RPCError(Faults.BAD_NAME, name)

    def removeProcessGroup(self, name):
        """ Remove a stopped process from the active configuration.

        @param string name         name of process group to remove
        @return boolean result     Indicates whether the removal was successful
        """
        self._update('removeProcessGroup')
        if name not in self.adminserviced.process_groups:
            raise RPCError(Faults.BAD_NAME, name)

        result = self.adminserviced.remove_process_group(name)
        if not result:
            raise RPCError(Faults.STILL_RUNNING, name)
        return True

    def _getAllProcesses(self, lexical=False, reverse=False):
        # if lexical is true, return processes sorted in lexical order,
        # otherwise, sort in priority order
        all_processes = []

        if lexical:
            group_names = self.adminserviced.process_groups.keys()
            group_names.sort()
            for group_name in group_names:
                group = self.adminserviced.process_groups[group_name]
                process_names = group.processes.keys()
                process_names.sort()
                if reverse:
                    process_names.reverse()
                for process_name in process_names:
                    process = group.processes[process_name]
                    all_processes.append((group, process))
        else:
            groups = self.adminserviced.process_groups.values()
            groups.sort() # asc by priority

            for group in groups:
                processes = group.processes.values()
                processes.sort() # asc by priority
                if reverse:
                    processes.reverse()
                for process in processes:
                    all_processes.append((group, process))

        return all_processes

    def _getGroupAndProcess(self, name):
        # get process to start from name
        group_name, process_name = split_namespec(name)

        group = self.adminserviced.process_groups.get(group_name)
        if group is None:
            raise RPCError(Faults.BAD_NAME, name)

        if process_name is None:
            return group, None

        process = group.processes.get(process_name)
        if process is None:
            raise RPCError(Faults.BAD_NAME, name)

        return group, process

    def startProcess(self, name, force, wait=True):
        """ Start a process

        @param string name Process name (or ``group:name``, or ``group:*``)
        @param string force Force the process to try start if its config is disabled
        @param boolean wait Wait for process to be fully started
        @return boolean result     Always true unless error

        """
        self._update('startProcess')
        group, process = self._getGroupAndProcess(name)
        if process is None:
            group_name, process_name = split_namespec(name)
            return self.startProcessGroup(group_name, force, wait)
        elif not boolean(force) and not process.config.is_enabled():
            return False

        if process.get_state() in RUNNING_STATES:
            raise RPCError(Faults.ALREADY_STARTED, name)
        elif boolean(force) and process.get_state() == ProcessStates.DISABLED:
            process.change_state(ProcessStates.STOPPED)

        # test filespec, don't bother trying to spawn if we know it will
        # eventually fail
        try:
            filename, argv = process.get_execv_args()
        except NotFound, why:
            raise RPCError(Faults.NO_FILE, why.args[0])
        except (NotExecutable, NoPermission), why:
            raise RPCError(Faults.NOT_EXECUTABLE, why.args[0])

        process.spawn()

        # We call reap() in order to more quickly obtain the side effects of
        # process.finish(), which reap() eventually ends up calling.  This
        # might be the case if the spawn() was successful but then the process
        # died before its startsecs elapsed or it exited with an unexpected
        # exit code. In particular, finish() may set spawnerr, which we can
        # check and immediately raise an RPCError, avoiding the need to
        # defer by returning a callback.

        self.adminserviced.reap()

        if process.spawnerr:
            raise RPCError(Faults.SPAWN_ERROR, name)

        # We call process.transition() in order to more quickly obtain its
        # side effects.  In particular, it might set the process' state from
        # STARTING->RUNNING if the process has a startsecs==0.
        process.transition()

        if wait and process.get_state() != ProcessStates.RUNNING:
            # by default, this branch will almost always be hit for processes
            # with default startsecs configurations, because the default number
            # of startsecs for a process is "1", and the process will not have
            # entered the RUNNING state yet even though we've called
            # transition() on it.  This is because a process is not considered
            # RUNNING until it has stayed up > startsecs.

            def onwait():
                if process.spawnerr:
                    raise RPCError(Faults.SPAWN_ERROR, name)

                state = process.get_state()

                if state not in (ProcessStates.STARTING, ProcessStates.RUNNING):
                    raise RPCError(Faults.ABNORMAL_TERMINATION, name)

                if state == ProcessStates.RUNNING:
                    return True

                return NOT_DONE_YET

            onwait.delay = 0.05
            onwait.rpcinterface = self
            return onwait # deferred

        return True

    def startProcessGroup(self, name, force, wait=True):
        """ Start all processes in the group named 'name'

        @param string name     The group name
        @param boolean wait    Wait for each process to be fully started
        @return array result   An array of process status info structs
        """
        self._update('startProcessGroup')

        group = self.adminserviced.process_groups.get(name)

        if group is None:
            raise RPCError(Faults.BAD_NAME, name)

        force = boolean(force) if force is not None else False
        processes = group.processes.values()
        processes.sort()
        processes = [ (group, process) for process in processes]

        startall = make_startallfunc(processes, isNotRunning, self.startProcess,
                                force=str(force), wait=wait)

        startall.delay = 0.05
        startall.rpcinterface = self
        return startall # deferred

    def startAllProcesses(self, wait=True):
        """ Start all processes listed in the configuration file

        @param boolean wait    Wait for each process to be fully started
        @return array result   An array of process status info structs
        """
        self._update('startAllProcesses')

        processes = self._getAllProcesses()
        startall = make_allfunc(processes, isNotRunning, self.startProcess,
                                force=False, wait=wait)

        startall.delay = 0.05
        startall.rpcinterface = self
        return startall # deferred

    def stopProcess(self, name, wait=True):
        """ Stop a process named by name

        @param string name  The name of the process to stop (or 'group:name')
        @param boolean wait        Wait for the process to be fully stopped
        @return boolean result     Always return True unless error
        """
        self._update('stopProcess')

        group, process = self._getGroupAndProcess(name)

        if process is None:
            group_name, process_name = split_namespec(name)
            return self.stopProcessGroup(group_name, wait)

        if process.get_state() not in RUNNING_STATES:
            raise RPCError(Faults.NOT_RUNNING, name)

        msg = process.stop()
        if msg is not None:
            raise RPCError(Faults.FAILED, msg)

        # We'll try to reap any killed child. FWIW, reap calls waitpid, and
        # then, if waitpid returns a pid, calls finish() on the process with
        # that pid, which drains any I/O from the process' dispatchers and
        # changes the process' state.  I chose to call reap without once=True
        # because we don't really care if we reap more than one child.  Even if
        # we only reap one child. we may not even be reaping the child that we
        # just stopped (this is all async, and process.stop() may not work, and
        # we'll need to wait for SIGKILL during process.transition() as the
        # result of normal select looping).

        self.adminserviced.reap()

        if wait and process.get_state() not in ALL_STOPPED_STATES:

            def onwait():
                # process will eventually enter a stopped state by
                # virtue of the adminserviced.reap() method being called
                # during normal operations
                process.stop_report()
                if process.get_state() not in ALL_STOPPED_STATES:
                    return NOT_DONE_YET
                return True

            onwait.delay = 0
            onwait.rpcinterface = self
            return onwait # deferred

        return True

    def updateProcessGroup(self, name, wait=True):
        """ Stop all processes in the process group named 'name'

        @param string name     The group name
        @param boolean wait    Wait for each process to be fully stopped
        @return array result   An array of process status info structs
        """
        self._update('updateProcessGroup')

        group = self.adminserviced.process_groups.get(name)

        if group is None:
            raise RPCError(Faults.BAD_NAME, name)

        # Get the new process configuration for the group
        config = None
        newProcs = []
        for cfg in self.adminserviced.options.process_group_configs:
            if cfg.name == name:
                config = cfg
                newProcs = [proc.name for proc in cfg.process_configs]
                break

        # If there's nothing, remove the group and return
        if len(newProcs) == 0:
            self.stopProcessGroup(name, wait)
            self.adminserviced.remove_process_group(group)
            return
        
        oldProcs = group.processes.values()
        oldProcs.sort()
        oldProcs.reverse()

        # Stop any processes that were in the old config, but aren't in the new one
        for proc in oldProcs:
            if not proc.config.name in newProcs and proc.state in RUNNING_STATES:
                self.adminserviced.options.logger.debug("Process %s is in state %d, stopping" % (proc.config.name, proc.state))
                self.stopProcess(proc.config.name, wait)

        # Save the new config and return the current process names
        config.after_setuid()
        self.adminserviced.process_groups[name] = config.make_group()
        procs = self.adminserviced.process_groups[name].processes.values()
        procs.sort()
        return [ proc.config.name for proc in procs ]

    def stopProcessGroup(self, name, wait=True):
        """ Stop all processes in the process group named 'name'

        @param string name     The group name
        @param boolean wait    Wait for each process to be fully stopped
        @return array result   An array of process status info structs
        """
        self._update('stopProcessGroup')

        group = self.adminserviced.process_groups.get(name)

        if group is None:
            raise RPCError(Faults.BAD_NAME, name)

        processes = group.processes.values()
        processes.sort()
        processes.reverse()
        processes = [ (group, process) for process in processes ]

        killall = make_allfunc(processes, isRunning, self.stopProcess,
                               wait=wait)

        killall.delay = 0.05
        killall.rpcinterface = self
        return killall # deferred

    def stopAllProcesses(self, wait=True):
        """ Stop all processes in the process list

        @param  boolean wait   Wait for each process to be fully stopped
        @return array result   An array of process status info structs
        """
        self._update('stopAllProcesses')

        processes = self._getAllProcesses(reverse=True)

        killall = make_allfunc(processes, isRunning, self.stopProcess,
                               wait=wait)

        killall.delay = 0.05
        killall.rpcinterface = self
        return killall # deferred

    def signalProcess(self, name, signal):
        """ Send an arbitrary UNIX signal to the process named by name

        @param string name    Name of the process to signal (or 'group:name')
        @param string signal  Signal to send, as name ('HUP') or number ('1')
        @return boolean
        """

        self._update('signalProcess')

        group, process = self._getGroupAndProcess(name)

        if process is None:
            group_name, process_name = split_namespec(name)
            return self.signalProcessGroup(group_name, signal=signal)

        try:
            sig = signal_number(signal)
        except ValueError:
            raise RPCError(Faults.BAD_SIGNAL, signal)

        if process.get_state() not in RUNNING_STATES:
            raise RPCError(Faults.NOT_RUNNING, name)

        msg = process.signal(sig)

        if not msg is None:
            raise RPCError(Faults.FAILED, msg)

        return True

    def signalProcessGroup(self, name, signal):
        """ Send a signal to all processes in the group named 'name'

        @param string name    The group name
        @param string signal  Signal to send, as name ('HUP') or number ('1')
        @return array
        """
        self._update('signalProcessGroup')

        group = self.adminserviced.process_groups.get(name)
        if group is None:
            raise RPCError(Faults.BAD_NAME, name)

        processes = group.processes.values()
        processes.sort()
        processes = [(group, process) for process in processes]

        sendall = make_allfunc(processes, isRunning, self.signalProcess,
                               signal=signal)
        result = sendall()
        self._update('signalProcessGroup')

        return result

    def signalAllProcesses(self, signal):
        """ Send a signal to all processes in the process list

        @param string signal  Signal to send, as name ('HUP') or number ('1')
        @return array         An array of process status info structs
        """
        processes = self._getAllProcesses()
        signalall = make_allfunc(processes, isRunning, self.signalProcess,
            signal=signal)
        result = signalall()
        self._update('signalAllProcesses')
        return result

    def getAllConfigInfo(self, full=False):
        """ Get info about all available process configurations. Each struct
        represents a single process (i.e. groups get flattened).

        @return array result  An array of process config info structs
        """
        self._update('getAllConfigInfo')
        return self.getConfigInfo(full, None)
    
    def getConfigInfo(self, full=False, proc=None):
        self._update('getConfigInfo')
        groupName = None
        procName = None
        if proc != None:
            groupName, procName = split_namespec(proc)

        configinfo = []
        for gconfig in self.adminserviced.options.process_group_configs:
            if groupName != None and gconfig.name != groupName:
                continue
            inuse = gconfig.name in self.adminserviced.process_groups
            for pconfig in gconfig.process_configs:
                if procName != None and pconfig.name != procName:
                    continue
                cfg = { 'name': pconfig.name,
                      'group': gconfig.name,
                      'inuse': inuse,
                      'autostart': pconfig.autostart,
                      'enabled': pconfig.is_enabled(),
                      'group_prio': gconfig.priority,
                      'process_prio': pconfig.priority }
                if full:
                    for key, val in pconfig.__dict__.items():
                        if val is not None:
                            cfg['config_%s' % key] = str(val)
                configinfo.append(cfg)

        configinfo.sort()
        return configinfo

    def _interpretProcessInfo(self, info):
        state = info['state']

        if state == ProcessStates.RUNNING:
            start = info['start']
            now = info['now']
            start_dt = datetime.datetime(*time.gmtime(start)[:6])
            now_dt = datetime.datetime(*time.gmtime(now)[:6])
            uptime = now_dt - start_dt
            if _total_seconds(uptime) < 0: # system time set back
                uptime = datetime.timedelta(0)
            desc = 'pid %s, uptime %s' % (info['pid'], uptime)

        elif state in (ProcessStates.FATAL, ProcessStates.BACKOFF):
            desc = info['spawnerr']
            if not desc:
                desc = 'unknown error (try "tail %s")' % info['name']

        elif state in (ProcessStates.DISABLED, ProcessStates.STOPPED, ProcessStates.EXITED):
            if info['start']:
                stop = info['stop']
                stop_dt = datetime.datetime(*time.localtime(stop)[:7])
                desc = stop_dt.strftime('%b %d %I:%M %p')
            else:
                desc = 'Not started'

        else:
            desc = ''

        return desc

    def getProcessInfo(self, name):
        """ Get info about a process named name

        @param string name The name of the process (or 'group:name')
        @return struct result     A structure containing data about the process
        """
        self._update('getProcessInfo')

        group, process = self._getGroupAndProcess(name)

        if process is None:
            raise RPCError(Faults.BAD_NAME, name)

        # TODO timestamps are returned as xml-rpc integers for b/c but will
        # saturate the xml-rpc integer type in jan 2038 ("year 2038 problem").
        # future api versions should return timestamps as a different type.
        start = capped_int(process.laststart)
        stop = capped_int(process.laststop)
        now = capped_int(self._now())

        state = process.get_state()
        spawnerr = process.spawnerr or ''
        exitstatus = process.exitstatus or 0
        stdout_logfile = process.config.stdout_logfile or ''
        stderr_logfile = process.config.stderr_logfile or ''

        info = {
            'name':process.config.name,
            'group':group.config.name,
            'start':start,
            'stop':stop,
            'now':now,
            'enabled':process.config.is_enabled(),
            'state':state,
            'statename':getProcessStateDescription(state),
            'spawnerr':spawnerr,
            'exitstatus':exitstatus,
            'logfile':stdout_logfile, # b/c alias
            'stdout_logfile':stdout_logfile,
            'stderr_logfile':stderr_logfile,
            'pid':process.pid,
            }

        description = self._interpretProcessInfo(info)
        info['description'] = description
        return info

    def _now(self): # pragma: no cover
        # this is here to service stubbing in unit tests
        return time.time()

    def getAllProcessInfo(self):
        """ Get info about all processes

        @return array result  An array of process status results
        """
        self._update('getAllProcessInfo')

        all_processes = self._getAllProcesses(lexical=True)

        output = []
        for group, process in all_processes:
            name = make_namespec(group.config.name, process.config.name)
            output.append(self.getProcessInfo(name))
        return output

    def _readProcessLog(self, name, offset, length, channel):
        group, process = self._getGroupAndProcess(name)

        if process is None:
            raise RPCError(Faults.BAD_NAME, name)

        logfile = getattr(process.config, '%s_logfile' % channel)

        if logfile is None or not os.path.exists(logfile):
            raise RPCError(Faults.NO_FILE, logfile)

        try:
            return readFile(logfile, int(offset), int(length))
        except ValueError, inst:
            why = inst.args[0]
            raise RPCError(getattr(Faults, why))

    def readProcessStdoutLog(self, name, offset, length):
        """ Read length bytes from name's stdout log starting at offset

        @param string name        the name of the process (or 'group:name')
        @param int offset         offset to start reading from.
        @param int length         number of bytes to read from the log.
        @return string result     Bytes of log
        """
        self._update('readProcessStdoutLog')
        return self._readProcessLog(name, offset, length, 'stdout')

    readProcessLog = readProcessStdoutLog # b/c alias

    def readProcessStderrLog(self, name, offset, length):
        """ Read length bytes from name's stderr log starting at offset

        @param string name        the name of the process (or 'group:name')
        @param int offset         offset to start reading from.
        @param int length         number of bytes to read from the log.
        @return string result     Bytes of log
        """
        self._update('readProcessStderrLog')
        return self._readProcessLog(name, offset, length, 'stderr')

    def _tailProcessLog(self, name, offset, length, channel):
        group, process = self._getGroupAndProcess(name)

        if process is None:
            raise RPCError(Faults.BAD_NAME, name)

        logfile = getattr(process.config, '%s_logfile' % channel)

        if logfile is None or not os.path.exists(logfile):
            return ['', 0, False]

        return tailFile(logfile, int(offset), int(length))

    def tailProcessStdoutLog(self, name, offset, length):
        """
        Provides a more efficient way to tail the (stdout) log than
        readProcessStdoutLog().  Use readProcessStdoutLog() to read
        chunks and tailProcessStdoutLog() to tail.

        Requests (length) bytes from the (name)'s log, starting at
        (offset).  If the total log size is greater than (offset +
        length), the overflow flag is set and the (offset) is
        automatically increased to position the buffer at the end of
        the log.  If less than (length) bytes are available, the
        maximum number of available bytes will be returned.  (offset)
        returned is always the last offset in the log +1.

        @param string name         the name of the process (or 'group:name')
        @param int offset          offset to start reading from
        @param int length          maximum number of bytes to return
        @return array result       [string bytes, int offset, bool overflow]
        """
        self._update('tailProcessStdoutLog')
        return self._tailProcessLog(name, offset, length, 'stdout')

    tailProcessLog = tailProcessStdoutLog # b/c alias

    def tailProcessStderrLog(self, name, offset, length):
        """
        Provides a more efficient way to tail the (stderr) log than
        readProcessStderrLog().  Use readProcessStderrLog() to read
        chunks and tailProcessStderrLog() to tail.

        Requests (length) bytes from the (name)'s log, starting at
        (offset).  If the total log size is greater than (offset +
        length), the overflow flag is set and the (offset) is
        automatically increased to position the buffer at the end of
        the log.  If less than (length) bytes are available, the
        maximum number of available bytes will be returned.  (offset)
        returned is always the last offset in the log +1.

        @param string name         the name of the process (or 'group:name')
        @param int offset          offset to start reading from
        @param int length          maximum number of bytes to return
        @return array result       [string bytes, int offset, bool overflow]
        """
        self._update('tailProcessStderrLog')
        return self._tailProcessLog(name, offset, length, 'stderr')

    def clearProcessLogs(self, name):
        """ Clear the stdout and stderr logs for the named process and
        reopen them.

        @param string name   The name of the process (or 'group:name')
        @return boolean result      Always True unless error
        """
        self._update('clearProcessLogs')

        group, process = self._getGroupAndProcess(name)

        if process is None:
            raise RPCError(Faults.BAD_NAME, name)

        try:
            # implies a reopen
            process.removelogs()
        except (IOError, OSError):
            raise RPCError(Faults.FAILED, name)

        return True

    clearProcessLog = clearProcessLogs # b/c alias

    def clearAllProcessLogs(self):
        """ Clear all process log files

        @return array result   An array of process status info structs
        """
        self._update('clearAllProcessLogs')
        results  = []
        callbacks = []

        all_processes = self._getAllProcesses()

        for group, process in all_processes:
            callbacks.append((group, process, self.clearProcessLog))

        def clearall():
            if not callbacks:
                return results

            group, process, callback = callbacks.pop(0)
            name = make_namespec(group.config.name, process.config.name)
            try:
                callback(name)
            except RPCError, e:
                results.append(
                    {'name':process.config.name,
                     'group':group.config.name,
                     'status':e.code,
                     'description':e.text})
            else:
                results.append(
                    {'name':process.config.name,
                     'group':group.config.name,
                     'status':Faults.SUCCESS,
                     'description':'OK'}
                    )

            if callbacks:
                return NOT_DONE_YET

            return results

        clearall.delay = 0.05
        clearall.rpcinterface = self
        return clearall # deferred

    def sendProcessStdin(self, name, chars):
        """ Send a string of chars to the stdin of the process name.
        If non-7-bit data is sent (unicode), it is encoded to utf-8
        before being sent to the process' stdin.  If chars is not a
        string or is not unicode, raise INCORRECT_PARAMETERS.  If the
        process is not running, raise NOT_RUNNING.  If the process'
        stdin cannot accept input (e.g. it was closed by the child
        process), raise NO_FILE.

        @param string name        The process name to send to (or 'group:name')
        @param string chars       The character data to send to the process
        @return boolean result    Always return True unless error
        """
        self._update('sendProcessStdin')

        if isinstance(chars, unicode):
            chars = chars.encode('utf-8')

        if not isinstance(chars, basestring):
            raise RPCError(Faults.INCORRECT_PARAMETERS, chars)

        group, process = self._getGroupAndProcess(name)

        if process is None:
            raise RPCError(Faults.BAD_NAME, name)

        if not process.pid or process.killing:
            raise RPCError(Faults.NOT_RUNNING, name)

        try:
            process.write(chars)
        except OSError, why:
            if why.args[0] == errno.EPIPE:
                raise RPCError(Faults.NO_FILE, name)
            else:
                raise

        return True

    def sendRemoteCommEvent(self, type, data):
        """ Send an event that will be received by event listener
        subprocesses subscribing to the RemoteCommunicationEvent.

        @param  string  type  String for the "type" key in the event header
        @param  string  data  Data for the event body
        @return boolean       Always return True unless error
        """
        if isinstance(type, unicode):
            type = type.encode('utf-8')
        if isinstance(data, unicode):
            data = data.encode('utf-8')

        notify(
            RemoteCommunicationEvent(type, data)
        )

        return True

def _total_seconds(timedelta):
    return ((timedelta.days * 86400 + timedelta.seconds) * 10**6 +
                timedelta.microseconds) / 10**6

def make_allfunc(processes, predicate, func, **extra_kwargs):
    """ Return a closure representing a function that calls a
    function for every process, and returns a result """

    callbacks = []
    results = []

    def allfunc(
        processes=processes,
        predicate=predicate,
        func=func,
        extra_kwargs=extra_kwargs,
        callbacks=callbacks, # used only to fool scoping, never passed by caller
        results=results, # used only to fool scoping, never passed by caller
        ):

        if not callbacks:

            for group, process in processes:
                name = make_namespec(group.config.name, process.config.name)
                if predicate(process):
                    try:
                        callback = func(name, **extra_kwargs)
                    except RPCError, e:
                        results.append({'name':process.config.name,
                                        'group':group.config.name,
                                        'status':e.code,
                                        'description':e.text})
                        continue
                    if isinstance(callback, types.FunctionType):
                        callbacks.append((group, process, callback))
                    else:
                        results.append(
                            {'name':process.config.name,
                             'group':group.config.name,
                             'status':Faults.SUCCESS,
                             'description':'OK'}
                            )

        if not callbacks:
            return results

        for struct in callbacks[:]:

            group, process, cb = struct

            try:
                value = cb()
            except RPCError, e:
                results.append(
                    {'name':process.config.name,
                     'group':group.config.name,
                     'status':e.code,
                     'description':e.text})
                callbacks.remove(struct)
            else:
                if value is not NOT_DONE_YET:
                    results.append(
                        {'name':process.config.name,
                         'group':group.config.name,
                         'status':Faults.SUCCESS,
                         'description':'OK'}
                        )
                    callbacks.remove(struct)

        if callbacks:
            return NOT_DONE_YET

        return results

    # XXX the above implementation has a weakness inasmuch as the
    # first call into each individual process callback will always
    # return NOT_DONE_YET, so they need to be called twice.  The
    # symptom of this is that calling this method causes the
    # client to block for much longer than it actually requires to
    # kill all of the running processes.  After the first call to
    # the killit callback, the process is actually dead, but the
    # above killall method processes the callbacks one at a time
    # during the select loop, which, because there is no output
    # from child processes after e.g. stopAllProcesses is called,
    # is not busy, so hits the timeout for each callback.  I
    # attempted to make this better, but the only way to make it
    # better assumes totally synchronous reaping of child
    # processes, which requires infrastructure changes to
    # adminserviced that are scary at the moment as it could take a
    # while to pin down all of the platform differences and might
    # require a C extension to the Python signal module to allow
    # the setting of ignore flags to signals.
    return allfunc

def make_startallfunc(processes, predicate, func, **extra_kwargs):
    """ Return a closure representing a function that calls a
    function for every process, and returns a result """

    callbacks = []
    results = []

    def startallfunc(
        processes=processes,
        predicate=predicate,
        func=func,
        extra_kwargs=extra_kwargs,
        callbacks=callbacks, # used only to fool scoping, never passed by caller
        results=results, # used only to fool scoping, never passed by caller
        ):

        if not callbacks:
            force = boolean(extra_kwargs['force'])
            last_state = None
            for group, process in processes:
                timed_out = False
                name = make_namespec(group.config.name, process.config.name)
                if predicate(process):
                    if not force and not process.config.is_enabled():
                        results.append({'name':process.config.name,
                                        'group':group.config.name,
                                        'status':Faults.DISABLED,
                                        'description':'%s is disabled' % (process.config.name)})
                        continue

                    # Remove the pid file, use its existence to check if the process has started
                    if process.config.pid_file and os.path.exists(process.config.pid_file) and not process.config.check_status():
                        print "Removing %s in make_startallfunc" % process.config.pid_file
                        os.remove(process.config.pid_file)
    
                    try:
                        if last_state is not None:
                            timed_out = wait_transition(last_state[0], last_state[1], process)
                        if timed_out:
                            results.append({'name':process.config.name,
                                            'group':group.config.name,
                                            'status':Faults.FAILED,
                                            'description':'Timed out waiting for previous %s to start' % (last_state[1].config.name)})
                            continue
                        if process.state == ProcessStates.DISABLED:
                            process.change_state(ProcessStates.STOPPED)
                        callback = func(name, **extra_kwargs)

                        last_state = (process.state, process)

                    except RPCError, e:
                        results.append({'name':process.config.name,
                                        'group':group.config.name,
                                        'status':e.code,
                                        'description':e.text})
                        continue
                    if isinstance(callback, types.FunctionType):
                        callbacks.append((group, process, callback))
                    else:
                        results.append(
                            {'name':process.config.name,
                             'group':group.config.name,
                             'status':Faults.SUCCESS,
                             'description':'OK'}
                            )

        if not callbacks:
            return results

        for struct in callbacks[:]:

            group, process, cb = struct

            try:
                value = cb()
            except RPCError, e:
                results.append(
                    {'name':process.config.name,
                     'group':group.config.name,
                     'status':e.code,
                     'description':e.text})
                callbacks.remove(struct)
            else:
                if value is not NOT_DONE_YET:
                    results.append(
                        {'name':process.config.name,
                         'group':group.config.name,
                         'status':Faults.SUCCESS,
                         'description':'OK'}
                        )
                    callbacks.remove(struct)

        if callbacks:
            return NOT_DONE_YET

        return results

    return startallfunc

def wait_transition(last_state, last_proc, new_proc):
    new_proc.config.options.logger.trace("RPC - %s - Last state: %s   wait: %r" % (new_proc.config.name, last_state, new_proc.config.waitforprevious))
    # The only states we want to start from: DISABLED(force was set true in the start proc), STOPPED or EXITED
    waitable_states = [ProcessStates.DISABLED, ProcessStates.STOPPED, ProcessStates.EXITED, ProcessStates.FATAL, ProcessStates.UNKNOWN]
    
    # Check if the new process' config has waiting enabled
    # - And the last state of the previous process is in STARTING
    # - And the current state is one we want to start from
    if new_proc.config.waitforprevious is not None and last_state == ProcessStates.STARTING and new_proc.get_state() in waitable_states:
        timed_out = new_proc.config.waitforprevious

        # Loop for new_proc.config.waitforprevious seconds, check each second
        while timed_out > 0:
            new_proc.config.options.logger.debug("%s is waiting for %s to start" % (new_proc.config.name, last_proc.config.name))
            time.sleep(1)
            timed_out -= 1

            if last_proc.get_state() == ProcessStates.STARTING:
                # "Help" the previous process along its way to the RUNNING state. If we don't
                # do this, the process won't transition on its own
                last_proc.transition()
            elif last_proc.get_state() == ProcessStates.RUNNING:
                new_proc.config.options.logger.debug("Last process started, going to start %s" % new_proc.config.name)
                return False

        # Check if the process transitioned to running on the last wait attempt
        if last_proc.get_state() != ProcessStates.RUNNING:
            new_proc.config.options.logger.debug("Last process did not start, returning %s for %s" % (new_proc.config.failafterwait, new_proc.config.name))
            return new_proc.config.failafterwait
        else:
            last_state = last_proc.get_state()

    new_proc.config.options.logger.debug("Not waiting anymore-- wait: %s  last_state: %s  new_state: %s" % (new_proc.config.waitforprevious, last_state, new_proc.get_state()))
    return False

def isRunning(process):
    if process.get_state() in RUNNING_STATES:
        return True
    else:
        process.config.options.logger.debug("%s is not running: %s" % (process.config.name, process.get_state()))

def isNotRunning(process):
    return not isRunning(process)

# this is not used in code but referenced via an entry point in the conf file
def make_main_rpcinterface(adminserviced):
    return AdminServiceNamespaceRPCInterface(adminserviced)

