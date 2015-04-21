import functools
import logging
from .infrastructure import Infrastructure
import core

logger = logging.getLogger(__name__)

EPHEMERAL = core.EPHEMERAL
PERMANENT = core.PERMANENT

def ephemeral(*args, **kwargs):
    # Determine whether or not the decorator was invoked.
    invoked = bool(args and not callable(args[0]) or kwargs)

    if not invoked:
        if isinstance(args[0], Infrastructure):
            # Set a wrapped Infractructure object to EPHEMERAL creation mode.
            infrastructure = args[0]
            infrastructure.category = EPHEMERAL
            decorator = infrastructure
        else:
            # Wrap a function in a decorator that sets the infrastructure creation mode to EPHEMERAL.
            function, args = args[0], ()
            @functools.wraps(function)
            def decorator(*args, **kwargs):
                core.CREATION_MODE = EPHEMERAL
                return function(*args, **kwargs)
            logging.info('Decorated (%s) as an \'Ephemeral\' Creation Mode function.' % function.__name__)
    else:
        # Define a decorator that will wrap a function in an Ephemeral Infrastructure object.
        def decorator(function):
            infrastructure = Infrastructure(function, *args, **kwargs)
            infrastructure.category = EPHEMERAL
            logging.info('Decorated (%s) as an Ephemeral Infrastructure object.' % function.__name__)
            return infrastructure

    return decorator

def permanent(*args, **kwargs):
    # Determine whether or not the decorator was invoked.
    invoked = bool(args and not callable(args[0]) or kwargs)

    if not invoked:
        if isinstance(args[0], Infrastructure):
            # Set a wrapped Infractructure object to PERMANENT creation mode.
            infrastructure = args[0]
            infrastructure.category = PERMANENT
            decorator = infrastructure
        else:
            # Wrap the function in a decorator that sets the infrastructure creation mode to PERMANENT.
            function, args = args[0], ()
            @functools.wraps(function)
            def decorator(*args, **kwargs):
                core.CREATION_MODE = PERMANENT
                return function(*args, **kwargs)
            logging.info('Decorated (%s) as a \'Permanent\' Creation Mode function.' % function.__name__)
    else:
        # Define a decorator that will wrap a function in an Permanent Infrastructure object.
        def decorator(function):
            infrastructure = Infrastructure(function, *args, **kwargs)
            infrastructure.category = PERMANENT
            logging.info('Decorated (%s) as an Permanent Infrastructure object.' % function.__name__)
            return infrastructure

    return decorator

def infrastructure(*args, **kwargs):

    # Determine whether or not the decorator was invoked.
    invoked = bool(args and not callable(args[0]) or kwargs)
    if not invoked:
        function, args = args[0], ()

    # Define decorator function.
    def decorator(function):
        return Infrastructure(function, *args, **kwargs)

    # If invoked, return the decorator function, which be called immediately. Otherwise, return the decorated function.
    return decorator if invoked else decorator(function)