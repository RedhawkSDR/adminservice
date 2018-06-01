#!/usr/bin/env python -u

"""rhadmin -- control applications run by adminserviced from the cmd line.

Usage: %s [options] [action [arguments]]

Options:
-c/--configuration FILENAME -- configuration file path (searches if not given)
-h/--help -- print usage message and exit
-i/--interactive -- start an interactive shell after executing commands
-s/--serverurl URL -- URL on which adminserviced server is listening
     (default "http://localhost:9001").
-u/--username USERNAME -- username to use for authentication with server
-p/--password PASSWORD -- password to use for authentication with server
-r/--history-file -- keep a readline history (if readline is available)

action [arguments] -- see below

Actions are commands like "tail" or "stop".  If -i is specified or no action is
specified on the command line, a "shell" interpreting actions typed
interactively is started.  Use the action "help" to find out about available
actions.
"""

import cmd
import errno
import getpass
import glob
import xmlrpclib
import socket
import urlparse
import re
import os
import sys
import threading
import time
import pkg_resources
import xml.dom.minidom
from operator import itemgetter

from adminservice.medusa import asyncore_25 as asyncore

from adminservice.datatypes import boolean
from adminservice.options import ClientOptions
from adminservice.options import make_namespec
from adminservice.options import readFile
from adminservice.options import split_namespec as sn
from adminservice import xmlrpc
from adminservice import states
from adminservice import http_client

class fgthread(threading.Thread):
    """ A subclass of threading.Thread, with a kill() method.
    To be used for foreground output/error streaming.
    http://mail.python.org/pipermail/python-list/2004-May/260937.html
    """

    def __init__(self, program, ctl):
        threading.Thread.__init__(self)
        self.killed = False
        self.program = program
        self.ctl = ctl
        self.listener = http_client.Listener()
        self.output_handler = http_client.HTTPHandler(self.listener,
                                                      self.ctl.options.username,
                                                      self.ctl.options.password)
        self.error_handler = http_client.HTTPHandler(self.listener,
                                                     self.ctl.options.username,
                                                     self.ctl.options.password)

    def start(self): # pragma: no cover
        # Start the thread
        self.__run_backup = self.run
        self.run = self.__run
        threading.Thread.start(self)

    def run(self): # pragma: no cover
        self.output_handler.get(self.ctl.options.serverurl,
                                '/logtail/%s/stdout' % self.program)
        self.error_handler.get(self.ctl.options.serverurl,
                               '/logtail/%s/stderr' % self.program)
        asyncore.loop()

    def __run(self): # pragma: no cover
        # Hacked run function, which installs the trace
        sys.settrace(self.globaltrace)
        self.__run_backup()
        self.run = self.__run_backup

    def globaltrace(self, frame, why, arg):
        if why == 'call':
            return self.localtrace
        else:
            return None

    def localtrace(self, frame, why, arg):
        if self.killed:
            if why == 'line':
                raise SystemExit()
        return self.localtrace

    def kill(self):
        self.output_handler.close()
        self.error_handler.close()
        self.killed = True

class Controller(cmd.Cmd):

    def __init__(self, options, completekey='tab', stdin=None,
                 stdout=None):
        self.options = options
        self.prompt = self.options.prompt + '> '
        self.options.plugins = []
        self.vocab = ['help']
        self._complete_info = None
        cmd.Cmd.__init__(self, completekey, stdin, stdout)
        for name, factory, kwargs in self.options.plugin_factories:
            plugin = factory(self, **kwargs)
            for a in dir(plugin):
                if a.startswith('do_') and callable(getattr(plugin, a)):
                    self.vocab.append(a[3:])
            self.options.plugins.append(plugin)
            plugin.name = name

    def emptyline(self):
        # We don't want a blank line to repeat the last command.
        return

    def exec_cmdloop(self, args, options):
        try:
            import readline
            delims = readline.get_completer_delims()
            delims = delims.replace(':', '')  # "group:process" as one word
            delims = delims.replace('*', '')  # "group:*" as one word
            delims = delims.replace('-', '')  # names with "-" as one word
            readline.set_completer_delims(delims)

            if options.history_file:
                try:
                    readline.read_history_file(options.history_file)
                except IOError:
                    pass

                def save():
                    try:
                        readline.write_history_file(options.history_file)
                    except IOError:
                        pass

                import atexit
                atexit.register(save)
        except ImportError:
            pass
        try:
            self.cmdqueue.append('status')
            self.cmdloop()
        except KeyboardInterrupt:
            self.output('')
            pass

    def onecmd(self, line):
        """ Override the onecmd method to:
          - catch and print all exceptions
          - allow for composite commands in interactive mode (foo; bar)
          - call 'do_foo' on plugins rather than ourself
        """
        origline = line
        lines = line.split(';') # don't filter(None, line.split), as we pop
        line = lines.pop(0)
        # stuffing the remainder into cmdqueue will cause cmdloop to
        # call us again for each command.
        self.cmdqueue.extend(lines)
        cmd, arg, line = self.parseline(line)
        if not line:
            return self.emptyline()
        if cmd is None:
            return self.default(line)
        self._complete_info = None
        self.lastcmd = line

        if cmd == '':
            return self.default(line)
        else:
            do_func = self._get_do_func(cmd)
            if do_func is None:
                return self.default(line)
            try:
                try:
                    return do_func(arg)
                except xmlrpclib.ProtocolError, e:
                    if e.errcode == 401:
                        if self.options.interactive:
                            self.output('Server requires authentication')
                            username = raw_input('Username:')
                            password = getpass.getpass(prompt='Password:')
                            self.output('')
                            self.options.username = username
                            self.options.password = password
                            return self.onecmd(origline)
                        else:
                            self.options.usage('Server requires authentication')
                    else:
                        raise
                do_func(arg)
            except SystemExit:
                raise
            except Exception:
                (file, fun, line), t, v, tbinfo = asyncore.compact_traceback()
                error = 'error: %s, %s: file: %s line: %s' % (t, v, file, line)
                self.output(error)
                if not self.options.interactive:
                    sys.exit(2)

    def _get_do_func(self, cmd):
        func_name = 'do_' + cmd
        func = getattr(self, func_name, None)
        if not func:
            for plugin in self.options.plugins:
                func = getattr(plugin, func_name, None)
                if func is not None:
                    break
        return func

    def output(self, message):
        if isinstance(message, unicode):
            message = message.encode('utf-8')
        self.stdout.write(message + '\n')

    def get_adminservice(self):
        return self.get_server_proxy('adminservice')

    def get_server_proxy(self, namespace=None):
        proxy = self.options.getServerProxy()
        if namespace is None:
            return proxy
        else:
            return getattr(proxy, namespace)

    def upcheck(self):
        try:
            adminservice = self.get_adminservice()
            api = adminservice.getVersion() # deprecated
            from adminservice import rpcinterface
            if api != rpcinterface.API_VERSION:
                self.output(
                    'Sorry, this version of rhadmin expects to '
                    'talk to a server with API version %s, but the '
                    'remote version is %s.' % (rpcinterface.API_VERSION, api))
                return False
        except xmlrpclib.Fault, e:
            if e.faultCode == xmlrpc.Faults.UNKNOWN_METHOD:
                self.output(
                    'Sorry, adminserviced responded but did not recognize '
                    'the adminservice namespace commands that rhadmin '
                    'uses to control it.  Please check that the '
                    '[rpcinterface:adminservice] section is enabled in the '
                    'configuration file (see sample.conf).')
                return False
            raise
        except socket.error, e:
            if e.args[0] == errno.ECONNREFUSED:
                self.output('%s refused connection' % self.options.serverurl)
                return False
            elif e.args[0] == errno.ENOENT:
                self.output('%s no such file' % self.options.serverurl)
                return False
            raise
        return True

    def complete(self, text, state, line=None):
        """Completer function that Cmd will register with readline using
        readline.set_completer().  This function will be called by readline
        as complete(text, state) where text is a fragment to complete and
        state is an integer (0..n).  Each call returns a string with a new
        completion.  When no more are available, None is returned."""
        if line is None: # line is only set in tests
            import readline
            line = readline.get_line_buffer()

        # take the last phrase from a line like "stop foo; start bar"
        phrase = line.split(';')[-1]

        matches = []
        # blank phrase completes to action list
        if not phrase.strip():
            matches = self._complete_actions(text)
        else:
            words = phrase.split()
            action = words[0]
            # incomplete action completes to action list
            if len(words) == 1 and not phrase.endswith(' '):
                matches = self._complete_actions(text)
            # actions that accept an action name
            elif action in ('help'):
                matches = self._complete_actions(text)
            # actions that accept a group name
            elif action in ('add', 'remove', 'update'):
                matches = self._complete_groups(text)
            # actions that accept a process name
            elif action in ('clear', 'fg', 'getconfig', 'pid', 'restart', 'signal',
                            'start', 'status', 'stop', 'tail'):
                matches = self._complete_processes(text)
            elif action in ('config'):
                matches = self._complete_config(text)
        if len(matches) > state:
            return matches[state]

    def _complete_actions(self, text):
        """Build a completion list of action names matching text"""
        return [ a + ' ' for a in self.vocab if a.startswith(text) ]

    def _complete_config(self, text):
        """Build a completion list of config names matching text"""
        config_opts = ['admin', 'domain', 'node', 'waveform']
        return [ a + ' ' for a in config_opts if a.startswith(text) ]

    def _complete_groups(self, text):
        """Build a completion list of group names matching text"""
        groups = []
        for info in self._get_complete_info():
            if info['group'] not in groups:
                groups.append(info['group'])
        return [ g + ' ' for g in groups if g.startswith(text) ]

    def _complete_processes(self, text):
        """Build a completion list of process names matching text"""
        processes = []
        for info in self._get_complete_info():
            if ':' in text or info['name'] != info['group']:
                processes.append('%s:%s' % (info['group'], info['name']))
                if '%s:*' % info['group'] not in processes:
                    processes.append('%s:*' % info['group'])
            else:
                processes.append(info['name'])
        return [ p + ' ' for p in processes if p.startswith(text) ]

    def _get_complete_info(self):
        """Get all process info used for completion.  We cache this between
        commands to reduce XML-RPC calls because readline may call
        complete() many times if the user hits tab only once."""
        if self._complete_info is None:
            self._complete_info = self.get_adminservice().getAllProcessInfo()
        return self._complete_info

    def do_help(self, arg):
        if arg.strip() == 'help':
            self.help_help()
        else:
            for plugin in self.options.plugins:
                plugin.do_help(arg)

    def help_help(self):
        self.output("help\t\tPrint a list of available actions")
        self.output("help <action>\tPrint help for <action>")

    def do_EOF(self, arg):
        self.output('')
        return 1

    def help_EOF(self):
        self.output("To quit, type ^D or use the quit command")

def get_names(inst):
    names = []
    classes = [inst.__class__]
    while classes:
        aclass = classes.pop(0)
        if aclass.__bases__:
            classes = classes + list(aclass.__bases__)
        names = names + dir(aclass)
    return names

class ControllerPluginBase:
    name = 'unnamed'

    def __init__(self, controller):
        self.ctl = controller

    def _doc_header(self):
        return "%s commands (type help <topic>):" % self.name
    doc_header = property(_doc_header)

    def do_help(self, arg):
        if arg:
            # XXX check arg syntax
            try:
                func = getattr(self, 'help_' + arg)
            except AttributeError:
                try:
                    doc = getattr(self, 'do_' + arg).__doc__
                    if doc:
                        self.ctl.output(doc)
                        return
                except AttributeError:
                    pass
                self.ctl.output(self.ctl.nohelp % (arg,))
                return
            func()
        else:
            names = get_names(self)
            cmds_doc = []
            cmds_undoc = []
            help = {}
            for name in names:
                if name[:5] == 'help_':
                    help[name[5:]]=1
            names.sort()
            # There can be duplicates if routines overridden
            prevname = ''
            for name in names:
                if name[:3] == 'do_':
                    if name == prevname:
                        continue
                    prevname = name
                    cmd=name[3:]
                    if cmd in help:
                        cmds_doc.append(cmd)
                        del help[cmd]
                    elif getattr(self, name).__doc__:
                        cmds_doc.append(cmd)
                    else:
                        cmds_undoc.append(cmd)
            self.ctl.output('')
            self.ctl.print_topics(self.doc_header, cmds_doc, 15, 80)

class DefaultControllerPlugin(ControllerPluginBase):
    name = 'default'
    listener = None # for unit tests
    def _tailf(self, path):
        self.ctl.output('==> Press Ctrl-C to exit <==')

        username = self.ctl.options.username
        password = self.ctl.options.password
        handler = None
        try:
            # Python's urllib2 (at least as of Python 2.4.2) isn't up
            # to this task; it doesn't actually implement a proper
            # HTTP/1.1 client that deals with chunked responses (it
            # always sends a Connection: close header).  We use a
            # homegrown client based on asyncore instead.  This makes
            # me sad.
            if self.listener is None:
                listener = http_client.Listener()
            else:
                listener = self.listener # for unit tests
            handler = http_client.HTTPHandler(listener, username, password)
            handler.get(self.ctl.options.serverurl, path)
            asyncore.loop()
        except KeyboardInterrupt:
            if handler:
                handler.close()
            self.ctl.output('')
            return

    def do_tail(self, arg):
        if not self.ctl.upcheck():
            return

        args = arg.split()

        if len(args) < 1:
            self.ctl.output('Error: too few arguments')
            self.help_tail()
            return

        elif len(args) > 3:
            self.ctl.output('Error: too many arguments')
            self.help_tail()
            return

        modifier = None

        if args[0].startswith('-'):
            modifier = args.pop(0)

        if len(args) == 1:
            name = args[-1]
            channel = 'stdout'
        else:
            if args:
                name = args[0]
                channel = args[-1].lower()
                if channel not in ('stderr', 'stdout'):
                    self.ctl.output('Error: bad channel %r' % channel)
                    return
            else:
                self.ctl.output('Error: tail requires process name')
                return

        bytes = 1600

        if modifier is not None:
            what = modifier[1:]
            if what == 'f':
                bytes = None
            else:
                try:
                    bytes = int(what)
                except:
                    self.ctl.output('Error: bad argument %s' % modifier)
                    return

        adminservice = self.ctl.get_adminservice()

        if bytes is None:
            return self._tailf('/logtail/%s/%s' % (name, channel))

        else:
            try:
                if channel is 'stdout':
                    output = adminservice.readProcessStdoutLog(name,
                                                             -bytes, 0)
                else: # if channel is 'stderr'
                    output = adminservice.readProcessStderrLog(name,
                                                             -bytes, 0)
            except xmlrpclib.Fault, e:
                template = '%s: ERROR (%s)'
                if e.faultCode == xmlrpc.Faults.NO_FILE:
                    self.ctl.output(template % (name, 'no log file'))
                elif e.faultCode == xmlrpc.Faults.FAILED:
                    self.ctl.output(template % (name,
                                             'unknown error reading log'))
                elif e.faultCode == xmlrpc.Faults.BAD_NAME:
                    self.ctl.output(template % (name,
                                             'no such process name'))
                else:
                    raise
            else:
                self.ctl.output(output)

    def help_tail(self):
        self.ctl.output(
            "tail [-f] <name> [stdout|stderr] (default stdout)\n"
            "Ex:\n"
            "tail -f <name>\t\tContinuous tail of named process stdout\n"
            "\t\t\tCtrl-C to exit.\n"
            "tail -100 <name>\tlast 100 *bytes* of process stdout\n"
            "tail <name> stderr\tlast 1600 *bytes* of process stderr"
            )

    def do_maintail(self, arg):
        if not self.ctl.upcheck():
            return

        args = arg.split()

        if len(args) > 1:
            self.ctl.output('Error: too many arguments')
            self.help_maintail()
            return

        elif len(args) == 1:
            if args[0].startswith('-'):
                what = args[0][1:]
                if what == 'f':
                    path = '/mainlogtail'
                    return self._tailf(path)
                try:
                    what = int(what)
                except:
                    self.ctl.output('Error: bad argument %s' % args[0])
                    return
                else:
                    bytes = what
            else:
                self.ctl.output('Error: bad argument %s' % args[0])
                return

        else:
            bytes = 1600

        adminservice = self.ctl.get_adminservice()

        try:
            output = adminservice.readLog(-bytes, 0)
        except xmlrpclib.Fault, e:
            template = '%s: ERROR (%s)'
            if e.faultCode == xmlrpc.Faults.NO_FILE:
                self.ctl.output(template % ('adminserviced', 'no log file'))
            elif e.faultCode == xmlrpc.Faults.FAILED:
                self.ctl.output(template % ('adminserviced',
                                         'unknown error reading log'))
            else:
                raise
        else:
            self.ctl.output(output)

    def help_maintail(self):
        self.ctl.output(
            "maintail -f \tContinuous tail of adminservice main log file"
            " (Ctrl-C to exit)\n"
            "maintail -100\tlast 100 *bytes* of adminserviced main log file\n"
            "maintail\tlast 1600 *bytes* of adminservice main log file\n"
            )

    def do_quit(self, arg):
        sys.exit(0)

    def help_quit(self):
        self.ctl.output("quit\tExit the adminservice shell.")

    do_exit = do_quit

    def help_exit(self):
        self.ctl.output("exit\tExit the adminservice shell.")

    def _show_statuses(self, process_infos, proc_type):
        namespecs, maxlen = [], 30
        for i, info in enumerate(process_infos):
            namespecs.append(make_namespec(info['group'], info['name']))
            if len(namespecs[i]) > maxlen:
                maxlen = len(namespecs[i])

        template = '%(namespec)-' + str(maxlen+3) + 's%(state)-10s%(desc)s'
        for i, info in enumerate(process_infos):
            if proc_type and not proc_type.startswith(info['config_type']):
                continue
            line = template % {'namespec': namespecs[i],
                               'state': info['statename'],
                               'desc': info['description']}
            self.ctl.output(line)

    def do_status(self, arg):
        if not self.ctl.upcheck():
            return

        adminservice = self.ctl.get_adminservice()
        all_infos = adminservice.getAllProcessInfo()

        type_to_status = ''
        names = arg.split()
        namelen = len(names)
        if namelen > 0 and names[0] in ['domain', 'nodes', 'waveforms', 'process']:
            type_to_status = names[0]
            names.remove(type_to_status)
        if namelen == 0 or "all" in names:
            matching_infos = all_infos
        else:
            matching_infos = []

            for name in names:
                bad_name = True
                group_name, process_name = split_namespec(name)

                for info in all_infos:
                    matched = info['group'] == group_name
                    if process_name is not None:
                        matched = matched and info['name'] == process_name

                    if matched:
                        bad_name = False
                        matching_infos.append(info)

                if bad_name:
                    if process_name is None:
                        msg = "%s: ERROR (no such group)" % group_name
                    else:
                        msg = "%s: ERROR (no such process)" % name
                    self.ctl.output(msg)
        self._show_statuses(matching_infos, type_to_status)

    def help_status(self):
        self.ctl.output("status <name>\t\tGet status for a single process")
        self.ctl.output("status <gname>:*\tGet status for all processes in a group")
        self.ctl.output("status <type> <gname>\t\tGet status for all processes of "
                        "<type> in a group where type is 'domain', 'nodes' or 'waveforms'")
        self.ctl.output("status <name> <name>\tGet status for multiple named processes")
        self.ctl.output("status\t\t\tGet all process status info")
        self.ctl.output("status <type> all\t\tGet status for all processes of <type> "
                        "where type is 'domain', 'nodes' or 'waveforms'")

    def do_pid(self, arg):
        adminservice = self.ctl.get_adminservice()
        if not self.ctl.upcheck():
            return
        names = arg.split()
        infos = []
        if not names:
            pid = adminservice.getPID()
            self.ctl.output("adminserviced: %s" % pid)
        elif 'all' in names:
            infos.extend(adminservice.getAllProcessInfo())
        else:
            for name in names:
                try:
                    info = adminservice.getProcessInfo(name)
                except xmlrpclib.Fault, e:
                    if e.faultCode == xmlrpc.Faults.BAD_NAME:
                        self.ctl.output('No such process %s' % name)
                    else:
                        raise
                else:
                    infos.append(info)
        self._show_pids(infos)
                    
    def _show_pids(self, process_infos):
        if len(process_infos) == 1:
            self.ctl.output('%s' % process_infos[0]['pid'])
            return

        namespecs, maxlen = [], 30
        for i, info in enumerate(process_infos):
            namespecs.append(make_namespec(info['group'], info['name']))
            if len(namespecs[i]) > maxlen:
                maxlen = len(namespecs[i])

        template = '%(namespec)-' + str(maxlen+3) + 's%(pid)-10s'
        for i, info in enumerate(process_infos):
            line = template % {'namespec': namespecs[i],
                               'pid': info['pid']}
            self.ctl.output(line)

    def help_pid(self):
        self.ctl.output("pid\t\t\tGet the PID of adminserviced.")
        self.ctl.output("pid <name>\t\tGet the PID of a single "
            "child process by name.")
        self.ctl.output("pid all\t\t\tGet the PID of every child "
            "process, one per line.")

    def _startresult(self, result):
        name = make_namespec(result['group'], result['name'])
        code = result['status']
        template = '%s: ERROR (%s)'
        if code == xmlrpc.Faults.BAD_NAME:
            return template % (name, 'no such process')
        elif code == xmlrpc.Faults.NO_FILE:
            return template % (name, 'no such file')
        elif code == xmlrpc.Faults.NOT_EXECUTABLE:
            return template % (name, 'file is not executable')
        elif code == xmlrpc.Faults.ALREADY_STARTED:
            return template % (name, 'already started')
        elif code == xmlrpc.Faults.SPAWN_ERROR:
            return template % (name, 'spawn error')
        elif code == xmlrpc.Faults.ABNORMAL_TERMINATION:
            return template % (name, 'abnormal termination')
        elif code == xmlrpc.Faults.FAILED:
            return template % (name, 'failed starting process')
        elif code == xmlrpc.Faults.DISABLED:
            return template % (name, 'process is disabled')
        elif code == xmlrpc.Faults.SUCCESS:
            return '%s: started' % name
        # assertion
        raise ValueError('Unknown result code %s for %s' % (code, name))

    def do_start(self, arg):
        if not self.ctl.upcheck():
            return

        names = arg.split()
        adminservice = self.ctl.get_adminservice()

        if not names:
            self.ctl.output("Error: start requires a process name")
            self.help_start()
            return

        force = 'False'
        type_to_start = ''
        if '-f' in names:
            force = 'True'
            names.remove('-f')
        if names[0] in ['domain', 'nodes', 'waveforms', 'process']:
            type_to_start = names[0]
            names.remove(type_to_start)
        if 'all' in names:
            results = adminservice.startAllProcesses(type_to_start)
            for result in results:
                result = self._startresult(result)
                self.ctl.output(result)

        else:
            for name in names:
                group_name, process_name = split_namespec(name)
                if process_name is None:
                    try:
                        results = adminservice.startProcessGroup(group_name, force, type_to_start)
                        for result in results:
                            result = self._startresult(result)
                            self.ctl.output(result)
                    except xmlrpclib.Fault, e:
                        if e.faultCode == xmlrpc.Faults.BAD_NAME:
                            error = "%s: ERROR (no such group)" % group_name
                            self.ctl.output(error)
                        else:
                            raise
                else:
                    proc_info = adminservice.getProcessInfo(name)
                    enabled = proc_info['enabled']
                    # print a message if this process won't be started or if it already is started
                    if proc_info['state'] in states.RUNNING_STATES:
                        self.ctl.output('%s is already running' % name)
                        continue
                    elif not enabled and not boolean(force):
                        self.ctl.output('%s is not enabled, use -f to force' % name)
                        continue
                    try:
                        result = adminservice.startProcess(name, force)
                    except xmlrpclib.Fault, e:
                        error = self._startresult({'status': e.faultCode,
                                                   'name': process_name,
                                                   'group': group_name,
                                                   'description': e.faultString})
                        self.ctl.output(error)
                    else:
                        name = make_namespec(group_name, process_name)
                        self.ctl.output('%s: started' % name)

    def help_start(self):
        self.ctl.output("start <name>\t\tStart a process")
        self.ctl.output("start <gname>:*\t\tStart all processes in a group")
        self.ctl.output("start <type> <gname>\t\tStart all processes of <type> "
                        "in a group where type is 'domain', 'nodes' or 'waveforms'")
        self.ctl.output("start <name> <name>\tStart multiple processes or groups")
        self.ctl.output("start all\t\tStart all processes")
        self.ctl.output("start <type> all\t\tStart all processes of <type> "
                        "where type is 'domain', 'nodes' or 'waveforms'")

    def _signalresult(self, result, success='signalled'):
        name = make_namespec(result['group'], result['name'])
        code = result['status']
        fault_string = result['description']
        template = '%s: ERROR (%s)'
        if code == xmlrpc.Faults.BAD_NAME:
            return template % (name, 'no such process')
        elif code == xmlrpc.Faults.BAD_SIGNAL:
            return template % (name, 'bad signal name')
        elif code == xmlrpc.Faults.NOT_RUNNING:
            return template % (name, 'not running')
        elif code == xmlrpc.Faults.SUCCESS:
            return '%s: %s' % (name, success)
        elif code == xmlrpc.Faults.FAILED:
            return fault_string
        # assertion
        raise ValueError('Unknown result code %s for %s' % (code, name))

    def _stopresult(self, result):
        return self._signalresult(result, success='stopped')

    def do_stop(self, arg):
        if not self.ctl.upcheck():
            return

        names = arg.split()
        adminservice = self.ctl.get_adminservice()

        if not names:
            self.ctl.output('Error: stop requires a process name')
            self.help_stop()
            return

        type_to_stop = ''
        if names[0] in ['domain', 'nodes', 'waveforms', 'process']:
            type_to_stop = names[0]
            names.remove(type_to_stop)
        if 'all' in names:
            results = adminservice.stopAllProcesses(type_to_stop)
            for result in results:
                result = self._stopresult(result)
                self.ctl.output(result)

        else:
            for name in names:
                group_name, process_name = split_namespec(name)
                if process_name is None:
                    try:
                        results = adminservice.stopProcessGroup(group_name, type_to_stop)
                        for result in results:
                            result = self._stopresult(result)
                            self.ctl.output(result)
                    except xmlrpclib.Fault, e:
                        if e.faultCode == xmlrpc.Faults.BAD_NAME:
                            error = "%s: ERROR (no such group)" % group_name
                            self.ctl.output(error)
                        else:
                            raise
                else:
                    try:
                        result = adminservice.stopProcess(name)
                    except xmlrpclib.Fault, e:
                        error = self._stopresult({'status': e.faultCode,
                                                  'name': process_name,
                                                  'group': group_name,
                                                  'description':e.faultString})
                        self.ctl.output(error)
                    else:
                        name = make_namespec(group_name, process_name)
                        self.ctl.output('%s: stopped' % name)

    def help_stop(self):
        self.ctl.output("stop <name>\t\tStop a process")
        self.ctl.output("stop <gname>:*\t\tStop all processes in a group")
        self.ctl.output("stop <type> <gname>\t\tStop all processes of <type> "
                        "in a group where type is 'domain', 'nodes' or 'waveforms'")
        self.ctl.output("stop <name> <name>\tStop multiple processes or groups")
        self.ctl.output("stop all\t\tStop all processes")
        self.ctl.output("stop <type> all\t\tStop all processes of <type> "
                        "where type is 'domain', 'nodes' or 'waveforms'")

    def do_signal(self, arg):
        if not self.ctl.upcheck():
            return

        args = arg.split()
        if len(args) < 2:
            self.ctl.output(
                'Error: signal requires a signal name and a process name')
            self.help_signal()
            return

        sig = args[0]
        names = args[1:]
        adminservice = self.ctl.get_adminservice()

        if 'all' in names:
            results = adminservice.signalAllProcesses(sig)
            for result in results:
                result = self._signalresult(result)
                self.ctl.output(result)

        else:
            for name in names:
                group_name, process_name = split_namespec(name)
                if process_name is None:
                    try:
                        results = adminservice.signalProcessGroup(
                            group_name, sig
                            )
                        for result in results:
                            result = self._signalresult(result)
                            self.ctl.output(result)
                    except xmlrpclib.Fault, e:
                        if e.faultCode == xmlrpc.Faults.BAD_NAME:
                            error = "%s: ERROR (no such group)" % group_name
                            self.ctl.output(error)
                        else:
                            raise
                else:
                    try:
                        adminservice.signalProcess(name, sig)
                    except xmlrpclib.Fault, e:
                        error = self._signalresult({'status': e.faultCode,
                                                    'name': process_name,
                                                    'group': group_name,
                                                    'description':e.faultString})
                        self.ctl.output(error)
                    else:
                        name = make_namespec(group_name, process_name)
                        self.ctl.output('%s: signalled' % name)

    def help_signal(self):
        self.ctl.output("signal <signal name> <name>\t\tSignal a process")
        self.ctl.output("signal <signal name> <gname>:*\t\tSignal all processes in a group")
        self.ctl.output("signal <signal name> <name> <name>\tSignal multiple processes or groups")
        self.ctl.output("signal <signal name> all\t\tSignal all processes")

    def do_restart(self, arg):
        if not self.ctl.upcheck():
            return

        names = arg.split()

        if not names:
            self.ctl.output('Error: restart requires a process name')
            self.help_restart()
            return

        self.do_stop(arg)
        self.do_start(arg)

    def help_restart(self):
        self.ctl.output("restart <name>\t\tRestart a process")
        self.ctl.output("restart <gname>:*\tRestart all processes in a group")
        self.ctl.output("restart <type> <gname>\t\tRestart all processes of <type> "
                        "in a group where type is 'domain', 'nodes' or 'waveforms'")
        self.ctl.output("restart <name> <name>\tRestart multiple processes or groups")
        self.ctl.output("restart all\t\tRestart all processes")
        self.ctl.output("restart <type> all\t\tRestart all processes of <type> "
                        "where type is 'domain', 'nodes' or 'waveforms'")
        self.ctl.output("Note: restart does not reread config files. For that,"
                        " see reread and update.")

    def do_shutdown(self, arg):
        if self.ctl.options.interactive:
            yesno = raw_input('Really shut the remote adminserviced process '
                              'down y/N? ')
            really = yesno.lower().startswith('y')
        else:
            really = 1

        if really:
            adminservice = self.ctl.get_adminservice()
            try:
                adminservice.shutdown()
            except xmlrpclib.Fault, e:
                if e.faultCode == xmlrpc.Faults.SHUTDOWN_STATE:
                    self.ctl.output('ERROR: already shutting down')
                else:
                    raise
            except socket.error, e:
                if e.args[0] == errno.ECONNREFUSED:
                    msg = 'ERROR: %s refused connection (already shut down?)'
                    self.ctl.output(msg % self.ctl.options.serverurl)
                elif e.args[0] == errno.ENOENT:
                    msg = 'ERROR: %s no such file (already shut down?)'
                    self.ctl.output(msg % self.ctl.options.serverurl)
                else:
                    raise
            else:
                self.ctl.output('Shut down')

    def help_shutdown(self):
        self.ctl.output("shutdown \tShut the remote adminserviced down.")

    def do_reload(self, arg):
        if arg:
            self.ctl.output('Error: reload accepts no arguments')
            self.help_reload()
            return

        if self.ctl.options.interactive:
            yesno = raw_input('Really restart the remote adminserviced process '
                              'y/N? ')
            really = yesno.lower().startswith('y')
        else:
            really = 1
        if really:
            adminservice = self.ctl.get_adminservice()
            try:
                adminservice.restart()
            except xmlrpclib.Fault, e:
                if e.faultCode == xmlrpc.Faults.SHUTDOWN_STATE:
                    self.ctl.output('ERROR: already shutting down')
                else:
                    raise
            else:
                self.ctl.output('Restarted adminserviced')

    def help_reload(self):
        self.ctl.output("reload \t\tRestart the remote adminserviced.")

    def _formatChanges(self, added_changed_dropped_tuple):
        added, changed, dropped = added_changed_dropped_tuple
        changedict = {}
        for n, t in [(added, 'available'),
                     (changed, 'changed'),
                     (dropped, 'disappeared')]:
            changedict.update(dict(zip(n, [t] * len(n))))

        if changedict:
            names = list(changedict.keys())
            names.sort()
            for name in names:
                self.ctl.output("%s: %s" % (name, changedict[name]))
        else:
            self.ctl.output("No config updates to processes")

    def _formatConfigInfo(self, configinfo):
        name = make_namespec(configinfo['group'], configinfo['name'])
        formatted = { 'name': name }
        if configinfo['inuse']:
            formatted['inuse'] = 'in use'
        else:
            formatted['inuse'] = 'avail'
        if configinfo['enabled']:
            formatted['enabled'] = 'Enabled' if boolean(configinfo['enabled']) else 'Disabled'
        else:
            formatted['enabled'] = 'Disabled'
        if configinfo['autostart']:
            formatted['autostart'] = 'auto'
        else:
            formatted['autostart'] = 'manual'
        formatted['priority'] = "%s:%s" % (configinfo['group_prio'],
                                           configinfo['process_prio'])

        template = '%(name)-32s %(inuse)-9s %(autostart)-9s %(enabled)-9s %(priority)s'
        return template % formatted

    def do_list(self, arg):
        if arg:
            self.ctl.output('Error: list accepts no arguments')
            self.help_list()
            return

        adminservice = self.ctl.get_adminservice()
        try:
            configinfo = adminservice.getAllConfigInfo()
        except xmlrpclib.Fault, e:
            if e.faultCode == xmlrpc.Faults.SHUTDOWN_STATE:
                self.ctl.output('ERROR: adminservice shutting down')
            else:
                raise
        else:
            configinfo = sorted(configinfo, key=itemgetter('group', 'name'))
            self.ctl.output('%-32s %-9s %-9s %-9s %s' % ('Name', 'In Use', 'Autostart', 'Enabled', 'Priority'))
            for pinfo in configinfo:
                self.ctl.output(self._formatConfigInfo(pinfo))

    def help_list(self):
        self.ctl.output("list\t\t\tDisplay all configured processes")

    def do_getconfig(self, arg):
        if not arg:
            self.ctl.output('Error: getconfig requires a process name')
            self.help_getconfig()
            return

        args = arg.split(' ')
        
        configproc = args[0]
        desiredconfigs = args[1] if len(args) > 1 else None

        adminservice = self.ctl.get_adminservice()
        try:
            configinfo = adminservice.getConfigInfo(True, configproc)
        except xmlrpclib.Fault, e:
            if e.faultCode == xmlrpc.Faults.SHUTDOWN_STATE:
                self.ctl.output('ERROR: adminservice shutting down')
            else:
                raise
        else:
            configinfo = sorted(configinfo, key=itemgetter('group', 'name'))
            for pinfo in configinfo:
                if not desiredconfigs: 
                    self.ctl.output("%s Configuration:" % configproc)
                cfgs = [(key[7:],val) for (key, val) in pinfo.items() if key.startswith("config_")]
                cfgs.sort()
                for key,val in cfgs:
                    if not desiredconfigs:
                        self.ctl.output('\t%-32s %s' % (key,val))
                    elif key in desiredconfigs:
                        self.ctl.output(val)


    def help_getconfig(self):
        self.ctl.output("getconfig\t\t\tDisplay the configuration of a process")

    def do_reread(self, arg):
        if arg:
            self.ctl.output('Error: reread accepts no arguments')
            self.help_reread()
            return

        adminservice = self.ctl.get_adminservice()
        try:
            result = adminservice.reloadConfig()
        except xmlrpclib.Fault, e:
            if e.faultCode == xmlrpc.Faults.SHUTDOWN_STATE:
                self.ctl.output('ERROR: adminservice shutting down')
            elif e.faultCode == xmlrpc.Faults.CANT_REREAD:
                self.ctl.output('ERROR: %s' % e.faultString)
            else:
                raise
        else:
            self._formatChanges(result[0])

    def help_reread(self):
        self.ctl.output("reread \t\t\tReload the daemon's configuration files without add/remove")

    def do_add(self, arg):
        names = arg.split()

        adminservice = self.ctl.get_adminservice()
        for name in names:
            try:
                adminservice.addProcessGroup(name)
            except xmlrpclib.Fault, e:
                if e.faultCode == xmlrpc.Faults.SHUTDOWN_STATE:
                    self.ctl.output('ERROR: shutting down')
                elif e.faultCode == xmlrpc.Faults.ALREADY_ADDED:
                    self.ctl.output('ERROR: process group already active')
                elif e.faultCode == xmlrpc.Faults.BAD_NAME:
                    self.ctl.output(
                        "ERROR: no such process/group: %s" % name)
                else:
                    raise
            else:
                self.ctl.output("%s: added process group" % name)

    def help_add(self):
        self.ctl.output("add <name> [...]\tActivates any updates in config "
                        "for process/group")

    def do_remove(self, arg):
        names = arg.split()

        adminservice = self.ctl.get_adminservice()
        for name in names:
            try:
                adminservice.removeProcessGroup(name)
            except xmlrpclib.Fault, e:
                if e.faultCode == xmlrpc.Faults.STILL_RUNNING:
                    self.ctl.output('ERROR: process/group still running: %s'
                                    % name)
                elif e.faultCode == xmlrpc.Faults.BAD_NAME:
                    self.ctl.output(
                        "ERROR: no such process/group: %s" % name)
                else:
                    raise
            else:
                self.ctl.output("%s: removed process group" % name)

    def help_remove(self):
        self.ctl.output("remove <name> [...]\tRemoves process/group from "
                        "active config")

    def do_update(self, arg):
        def log(name, message):
            self.ctl.output("%s: %s" % (name, message))

        adminservice = self.ctl.get_adminservice()
        try:
            result = adminservice.reloadConfig()
        except xmlrpclib.Fault, e:
            if e.faultCode == xmlrpc.Faults.SHUTDOWN_STATE:
                self.ctl.output('ERROR: already shutting down')
                return
            else:
                raise

        added, changed, removed = result[0]
        valid_gnames = set(arg.split())

        # If all is specified treat it as if nothing was specified.
        if "all" in valid_gnames:
            valid_gnames = set()

        # If any gnames are specified we need to verify that they are
        # valid in order to print a useful error message.
        if valid_gnames:
            groups = set()
            for info in adminservice.getAllProcessInfo():
                groups.add(info['group'])
            # New gnames would not currently exist in this set so
            # add those as well.
            groups.update(added)

            for gname in valid_gnames:
                if gname not in groups:
                    self.ctl.output('ERROR: no such group: %s' % gname)

        for gname in removed:
            if valid_gnames and gname not in valid_gnames:
                continue
            results = adminservice.stopProcessGroup(gname)
            log(gname, "stopped")

            fails = [res for res in results
                     if res['status'] == xmlrpc.Faults.FAILED]
            if fails:
                log(gname, "has problems; not removing")
                continue
            adminservice.removeProcessGroup(gname)
            log(gname, "removed process group")

        waitfor = {}
        for gname in changed:
            if valid_gnames and gname not in valid_gnames:
                continue
            # Update the group and have the process definitions re-run their transitions
            results = adminservice.updateProcessGroup(gname)
            # Keep track of the processes for the group, need to watch them start
            waitfor[gname] = results
            log(gname, "updated process group")

        for gname in added:
            if valid_gnames and gname not in valid_gnames:
                continue
            adminservice.addProcessGroup(gname)
            log(gname, "added process group")

        # Loop through all the groups that we've changed in the order that we changed them
        for gname in changed:
            if not waitfor.has_key(gname):
                continue
            procs = waitfor[gname]
            if procs is not None:
                # Loop through each process and get its status
                for proc in procs:
                    procInfo = adminservice.getProcessInfo("%s:%s" % (gname, proc))
                    procName = procInfo['name']
                    state = procInfo['state']

                    if state == states.ProcessStates.STARTING:
                        self.ctl.output("Waiting on %s to start" % procName)

                    # If the process is in the starting state, watch until it's not in that state anymore
                    while state in [states.ProcessStates.STARTING]:
                        time.sleep(2)
                        procInfo = adminservice.getProcessInfo("%s:%s" % (gname, proc))
                        state = procInfo['state']

                    if state == states.ProcessStates.RUNNING:
                        self.ctl.output("%s started" % procName)
                        continue
                    elif not state in [states.ProcessStates.STOPPED, states.ProcessStates.DISABLED]:
                        self.ctl.output("%s didn't start, state is %s" % (procName, states.getProcessStateDescription(state)))

        # Loop through all the defined processes and make sure the disabled ones are stopped
        for info in adminservice.getAllProcessInfo():
            if info['state'] in states.RUNNING_STATES and not info['enabled']:
                group = info['group']
                name = ('%s:' % group if group else '') + info['name']

                self.ctl.output("%s shouldn't be running, stopping" % name)
                adminservice.stopProcess(name)

    def help_update(self):
        self.ctl.output("update\t\t\tReload config and add/remove as necessary, and will restart affected programs")
        self.ctl.output("update all\t\tReload config and add/remove as necessary, and will restart affected programs")
        self.ctl.output("update <gname> [...]\tUpdate specific groups")

    def _clearresult(self, result):
        name = make_namespec(result['group'], result['name'])
        code = result['status']
        template = '%s: ERROR (%s)'
        if code == xmlrpc.Faults.BAD_NAME:
            return template % (name, 'no such process')
        elif code == xmlrpc.Faults.FAILED:
            return template % (name, 'failed')
        elif code == xmlrpc.Faults.SUCCESS:
            return '%s: cleared' % name
        raise ValueError('Unknown result code %s for %s' % (code, name))

    def do_clear(self, arg):
        if not self.ctl.upcheck():
            return

        names = arg.split()

        if not names:
            self.ctl.output('Error: clear requires a process name')
            self.help_clear()
            return

        adminservice = self.ctl.get_adminservice()

        if 'all' in names:
            results = adminservice.clearAllProcessLogs()
            for result in results:
                result = self._clearresult(result)
                self.ctl.output(result)
        else:
            for name in names:
                group_name, process_name = split_namespec(name)
                try:
                    result = adminservice.clearProcessLogs(name)
                except xmlrpclib.Fault, e:
                    error = self._clearresult({'status': e.faultCode,
                                               'name': process_name,
                                               'group': group_name,
                                               'description': e.faultString})
                    self.ctl.output(error)
                else:
                    name = make_namespec(group_name, process_name)
                    self.ctl.output('%s: cleared' % name)

    def help_clear(self):
        self.ctl.output("clear <name>\t\tClear a process' log files.")
        self.ctl.output(
            "clear <name> <name>\tClear multiple process' log files")
        self.ctl.output("clear all\t\tClear all process' log files")

    def _queryresult(self, result):
        name = make_namespec(result['group'], result['name'])
        output = result['output'] if result.has_key('output') else result['description']
        template = '%s: %s\n'
        return template % (name, output)

    def do_query(self, arg):
        if not self.ctl.upcheck():
            return

        names = arg.split()
        adminservice = self.ctl.get_adminservice()

        if not names:
            self.ctl.output("Error: query requires a process name")
            self.help_start()
            return

        type_to_query = ''
        if names[0] in ['domain', 'nodes', 'waveforms', 'process']:
            type_to_query = names[0]
            names.remove(type_to_query)
        if 'all' in names:
            results = adminservice.queryAllProcesses(type_to_query)
            for result in results:
                result = self._queryresult(result)
                self.ctl.output(result)

        else:
            for name in names:
                group_name, process_name = split_namespec(name)
                if process_name is None:
                    try:
                        results = adminservice.queryProcessGroup(group_name, type_to_query)
                        for result in results:
                            self.ctl.output(result['output'])
                    except xmlrpclib.Fault, e:
                        if e.faultCode == xmlrpc.Faults.BAD_NAME:
                            error = "%s: ERROR (no such group)" % group_name
                            self.ctl.output(error)
                        else:
                            raise
                else:
                    try:
                        result = adminservice.queryProcess(name)
                        self.ctl.output(result)
                    except xmlrpclib.Fault, e:
                        error = self._queryresult({'name': process_name,
                                                   'group': group_name,
                                                   'description': e.faultString})
                        self.ctl.output(error)

    def help_query(self):
        self.ctl.output("query <name>\t\tQuery a process")
        self.ctl.output("query <gname>:*\t\tQuery all processes in a group")
        self.ctl.output("query <type> <gname>\t\tQuery all processes of <type> "
                        "in a group where type is 'domain', 'nodes' or 'waveforms'")
        self.ctl.output("query <name> <name>\tQuery multiple processes or groups")
        self.ctl.output("query all\t\tQuery all processes")
        self.ctl.output("query <type> all\t\tQuery all processes of <type> "
                        "where type is 'domain', 'nodes' or 'waveforms'")

    def do_open(self, arg):
        url = arg.strip()
        parts = urlparse.urlparse(url)
        if parts[0] not in ('unix', 'http'):
            self.ctl.output('ERROR: url must be http:// or unix://')
            return
        self.ctl.options.serverurl = url
        self.do_status('')

    def help_open(self):
        self.ctl.output("open <url>\tConnect to a remote adminserviced process.")
        self.ctl.output("\t\t(for UNIX domain socket, use unix:///socket/path)")

    def do_version(self, arg):
        if arg:
            self.ctl.output('Error: version accepts no arguments')
            self.help_version()
            return

        if not self.ctl.upcheck():
            return
        adminservice = self.ctl.get_adminservice()
        self.ctl.output(adminservice.getAdminServiceVersion())

    def help_version(self):
        self.ctl.output(
            "version\t\t\tShow the version of the remote adminserviced "
            "process")

    def do_fg(self, arg):
        if not self.ctl.upcheck():
            return

        names = arg.split()
        if not names:
            self.ctl.output('ERROR: no process name supplied')
            self.help_fg()
            return
        if len(names) > 1:
            self.ctl.output('ERROR: too many process names supplied')
            return

        name = names[0]
        adminservice = self.ctl.get_adminservice()

        try:
            info = adminservice.getProcessInfo(name)
        except xmlrpclib.Fault, e:
            if e.faultCode == xmlrpc.Faults.BAD_NAME:
                self.ctl.output('ERROR: bad process name supplied')
            else:
                self.ctl.output('ERROR: ' + str(e))
            return

        if info['state'] != states.ProcessStates.RUNNING:
            self.ctl.output('ERROR: process not running')
            return

        self.ctl.output('==> Press Ctrl-C to exit <==')

        a = None
        try:
            # this thread takes care of the output/error messages
            a = fgthread(name, self.ctl)
            a.start()

            # this takes care of the user input
            while True:
                inp = raw_input() + '\n'
                try:
                    adminservice.sendProcessStdin(name, inp)
                except xmlrpclib.Fault, e:
                    if e.faultCode == xmlrpc.Faults.NOT_RUNNING:
                        self.ctl.output('Process got killed')
                    else:
                        self.ctl.output('ERROR: ' + str(e))
                    self.ctl.output('Exiting foreground')
                    a.kill()
                    return

                info = adminservice.getProcessInfo(name)
                if info['state'] != states.ProcessStates.RUNNING:
                    self.ctl.output('Process got killed')
                    self.ctl.output('Exiting foreground')
                    a.kill()
                    return
        except (KeyboardInterrupt, EOFError):
            self.ctl.output('Exiting foreground')
            if a:
                a.kill()

    def help_fg(self,args=None):
        self.ctl.output('fg <process>\tConnect to a process in foreground mode')
        self.ctl.output("\t\tCtrl-C to exit")

    config_files = {
        'admin'      : 'skel/sample.admin',
        'domain'     : 'skel/sample.domains',
        'node'       : 'skel/sample.nodes',
        'waveform'   : 'skel/sample.waveforms',
        'supervisor' : 'skel/sample.conf'
        }

    def do_config(self, arg):
        args = arg.split(' ')
        if len(args) == 0 or not self.config_files.has_key(args[0]):
            self.ctl.output('Error: incorrect config argument')
            self.help_config()
            return
        config = self.process_config_args(args)
        
        self.ctl.output(config)

    def help_config(self):
        self.ctl.output('config <type> <xml file> <domain name>\tOutput a config file for the specified type to the screen')
        self.ctl.output("\t\t<type>\tcan be one of: admin, domain, node, waveform")
        self.ctl.output("\t\t<xml file>\tthe relative or absolute path to the .dcd.xml or .sad.xml file for the type specified")
        self.ctl.output("\t\t<domain name>\toptional - the domain name to use for the node or waveform config file")

    def process_config_args(self, args):
        config = pkg_resources.resource_string(__name__, self.config_files[args[0]])
        if len(args) > 1:
            if 'node' == args[0]:
                config = self.process_node_file(args)
            elif 'domain' == args[0]:
                config = self.process_domain_file(args)
            elif 'waveform' == args[0]:
                config = self.process_waveform_file(args)
                
        return config

    def process_domain_file(self, args):
        config = '[domain:domain1]\nDOMAIN_NAME= \n'
        dmdFile = args[1]
        docDmd = xml.dom.minidom.parse(dmdFile)

        # Grab the first .ini file in the same directory as the dmd.xml
        iniFiles = glob.glob(os.path.join(os.path.split(dmdFile)[0], '*.ini'))
        if len(iniFiles) > 0:
            iniContents = readFile(iniFiles[0], 0, 0)
            if len(iniContents) > 0:
                config = iniContents

        # Find the DMD associated with the domain
        domainName = docDmd.getElementsByTagName('domainmanagerconfiguration')[0].getAttribute('name')

        config = re.sub(r"\[domain:.*\]", '[domain:%s_mgr]' % domainName, config)
        config = re.sub(r"DOMAIN_NAME=.*?([;\n])", 'DOMAIN_NAME=%s \\1' % domainName, config)

        return config

    def process_node_file(self, args):
        config = '[node:node1]\nDOMAIN_NAME= \nNODE_NAME= \nDCD_FILE= \n'
        dcdFile = args[1]
        docDcd = xml.dom.minidom.parse(dcdFile)
        
        # Grab the first .ini file in the same directory as the dcd.xml
        iniFiles = glob.glob(os.path.join(os.path.split(dcdFile)[0], '*.ini'))
        if len(iniFiles) > 0:
            iniContents = readFile(iniFiles[0], 0, 0)
            if len(iniContents) > 0:
                config = iniContents
        
        # Find the DCD associated with the node
        nodeName = docDcd.getElementsByTagName('deviceconfiguration')[0].getAttribute('name')
        domainName = docDcd.getElementsByTagName('deviceconfiguration')[0].getElementsByTagName('domainmanager')[0].getElementsByTagName('namingservice')[0].getAttribute('name').split('/')[0]
        if len(args) == 3:
            domainName = args[2]

        config = re.sub(r"\[node:.*\]", '[node:%s]' % nodeName, config)
        config = re.sub(r"DOMAIN_NAME=.*?([;\n])", 'DOMAIN_NAME=%s \\1' % domainName, config)
        config = re.sub(r"NODE_NAME=.*?([;\n])", 'NODE_NAME=%s \\1' % nodeName, config)
        config = re.sub(r"DCD_FILE=.*?([;\n])", 'DCD_FILE=/nodes/%s/DeviceManager.dcd.xml \\1' % nodeName, config)

        return config

    def process_waveform_file(self, args):
        config = '[waveform:waveform1]\nDOMAIN_NAME= \nWAVEFORM= \n'
        sadFile = args[1]
        docSad = xml.dom.minidom.parse(args[1])
        
        # Grab the first .ini file in the same directory as the sad.xml
        iniFiles = glob.glob(os.path.join(os.path.split(sadFile)[0], '*.ini'))
        if len(iniFiles) > 0:
            iniContents = readFile(iniFiles[0], 0, 0)
            if len(iniContents) > 0:
                config = iniContents

        # Find the SAD associated with the waveform
        waveformName = docSad.getElementsByTagName('softwareassembly')[0].getAttribute('name')

        config = re.sub(r"\[waveform:.*\]", '[waveform:%s]' % waveformName, config)
        config = re.sub(r"WAVEFORM=.*?([;\n])", 'WAVEFORM=%s \\1' % waveformName, config)
        if len(args) == 3:
            config = re.sub(r"DOMAIN_NAME=.*?([;\n])", 'DOMAIN_NAME=%s \\1' % args[2], config)

        return config

def split_namespec(name):
    if name.find(":") != -1:
        return sn(name)
    return name, None

def main(args=None, options=None):
    if options is None:
        options = ClientOptions()

    options.realize(args, doc=__doc__)
    c = Controller(options)

    if options.args:
        c.onecmd(" ".join(options.args))

    if options.interactive:
        c.exec_cmdloop(args, options)


if __name__ == "__main__":
    main()
