# This module must not depend on any other non-stdlib module to prevent
# circular import problems.

class ProcessStates:
    DISABLED = -10
    STOPPED = 0
    STARTING = 10
    RUNNING = 20
    BACKOFF = 30
    STOPPING = 40
    EXITED = 100
    FATAL = 200
    UNKNOWN = 1000

ALL_STOPPED_STATES = (ProcessStates.DISABLED,
                  ProcessStates.STOPPED,
                  ProcessStates.EXITED,
                  ProcessStates.FATAL,
                  ProcessStates.UNKNOWN)

STOPPED_STATES = (ProcessStates.STOPPED,
                  ProcessStates.EXITED,
                  ProcessStates.FATAL,
                  ProcessStates.UNKNOWN)

RUNNING_STATES = (ProcessStates.RUNNING,
                  ProcessStates.BACKOFF,
                  ProcessStates.STARTING)

def getProcessStateDescription(code):
    return _process_states_by_code.get(code)


class AdminServiceStates:
    FATAL = 2
    RUNNING = 1
    RESTARTING = 0
    SHUTDOWN = -1

def getAdminServiceStateDescription(code):
    return _adminservice_states_by_code.get(code)


class EventListenerStates:
    READY = 10 # the process ready to be sent an event from adminservice
    BUSY = 20 # event listener is processing an event sent to it by adminservice
    ACKNOWLEDGED = 30 # the event listener processed an event
    UNKNOWN = 40 # the event listener is in an unknown state

def getEventListenerStateDescription(code):
    return _eventlistener_states_by_code.get(code)


# below is an optimization for internal use in this module only
def _names_by_code(states):
    d = {}
    for name in states.__dict__:
        if not name.startswith('__'):
            code = getattr(states, name)
            d[code] = name
    return d
_process_states_by_code = _names_by_code(ProcessStates)
_adminservice_states_by_code = _names_by_code(AdminServiceStates)
_eventlistener_states_by_code = _names_by_code(EventListenerStates)
