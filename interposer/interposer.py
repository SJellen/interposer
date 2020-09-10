# -*- coding: utf-8 -*-
#
# Copyright (C) 2019 - 2020 Tuono, Inc.
# All Rights Reserved
#
import json
import logging
import shelve
import types

from contextlib import AbstractContextManager
from datetime import datetime
from enum import auto
from enum import Enum
from hashlib import sha1
from pathlib import Path
from pprint import pformat
from typing import Dict

from wrapt import CallableObjectProxy

from .errors import PlaybackError
from .errors import WrappingError


class Mode(Enum):
    """
    The running mode of the interposer.

    In Recording mode, method and function calls get recorded.
    In Playback mode, method and function calls get played back.
    """

    Playback = auto()
    Recording = auto()


class InterposerEncoder(json.JSONEncoder):
    """
    Handles conversion of commonly used types not normally convertible
    to JSON, such as datetime and enumerations.  This is the conversion
    that parameters (*args, **kwargs) passed to a method go through
    before they are hashed.
    """

    def default(self, obj):
        """
        If we get here, the standard processor was unable to convert the
        content to JSON.  We're the last thing standing in the way of a
        successful conversion.
        """
        if isinstance(obj, datetime):
            return str(obj)
        elif isinstance(obj, Enum):
            return obj.value

        # If we get here, all hope is lost; let the base class raise
        return super().default(obj)


class Interposer(object):
    """
    Record any function calls and play back the result later.

    The interposer is useful where you are dealing with a third party
    library and you would like to:

      - Occasionally ensure your code works live,
      - Record detailed responses from third party libraries instead
        of mocking them,
      - Always ensure your code works.

    Recording has advantages and disadvantages, so the right solution
    for your situation depends on many things.  Recording eliminates
    the need to produce and maintain mocks of third party libraries.
    Mocks of third party libraries that change or are not well
    understood are fragile and lead to a false sense of safety.
    Recordings on the other hand are always correct, but they need to
    be regenerated when your logic changes around the third party calls,
    or when the third party changes.

    Recording file format history:
      -  1: code did not record exceptions
      -  2: added exception recording and playback support
      -  3: renamed "context" to "channel" maintaining compatibility with v2 recordings
      -  4: ordinal counting of calls for linear playback
      -  5: support datetime and enum in argument lists
    """

    VERSION = 5

    def __init__(
        self,
        datafile: Path,
        mode: Mode,
        encoder: json.JSONEncoder = InterposerEncoder,
        loglevels: Dict[str, Dict[str, int]] = {
            # opening and closing files
            "fileio": {"open": logging.INFO, "close": logging.INFO},
            # wrapping: NOISY!
            "wrappr": {"call": logging.DEBUG, "wrap": logging.DEBUG},
            # processing calls
            "except": {"playback": logging.DEBUG, "recorded": logging.DEBUG},
            "params": {"playback": logging.DEBUG, "recorded": logging.DEBUG},
            "result": {"playback": logging.DEBUG, "recorded": logging.DEBUG},
        },
        logprefix: str = "TAPE: ",
    ):
        """
        Initializer.

        Attributes:
          datafile (Path): The full path to the recording filename.
          mode (Mode): The operational mode - Playback or Recording.
          encoder (json.JSONEncoder): parameter encoder to use for hashing
          loglevels (Dict): logging level controls
          logprefix (str): common prefix for all interposer log messages
        """
        self.call_order = {}
        self.deck = datafile
        self.encoder = encoder
        self.logger = logging.getLogger(__name__)
        self.loglevels = loglevels
        self.logprefix = logprefix
        self.mode = mode
        self.playback_call_order = {}
        self.playback_index = {}
        self.tape = None
        self.version = self.VERSION

    def open(self):
        if not self.tape:
            if self.mode == Mode.Playback:
                # Open db file read-only
                self.tape = shelve.open(str(self.deck), flag="r", protocol=4)
                self.version = self.tape.get("_version", 1)
                # Load the call order if present
                if "deck_call_order" in self.tape:
                    self.call_order = self.tape["deck_call_order"]
            else:
                # Open db file rw, and create if it doesn't exist
                self.tape = shelve.open(str(self.deck), flag="c", protocol=4)
                self.tape["_version"] = self.VERSION

            self._log(
                "fileio",
                "open",
                f"opened {self.deck} for {self.mode} using version {self.version}",
            )

    def close(self):
        if self.tape:
            if self.mode != Mode.Playback:
                self.tape["deck_call_order"] = self.call_order
            self.tape.close()
            self.tape = None
            self._log(
                "fileio",
                "close",
                f"closed {self.deck} for {self.mode} using version {self.version}",
            )

    def wrap(self, thing, channel="default") -> "_InterposerWrapper":
        """
        Wrap something with the interposer.

        Modules are wrappable, then getattr to wrap...
        Class definitions are wrappable, then called to wrap...
        Class instantiations are wrappable, then getattr to wrap...
        Class properties return potentially wrappable objects.
        Class methods are wrappable and their calls get intercepted.
        Bare function (and lambda) get intercepted.

        Arguments:
          thing: the thing to wrap
          channel: the channel name.  If no channel is specified, everything is
                   placed into a channel named "default".

        Raises:
          WrappingError when the requested thing is not wrappable.
        """
        if not self.wrappable(thing):
            raise WrappingError(thing)
        self._log("wrappr", "wrap", f"wrapped {thing}")
        return _InterposerWrapper(self, thing, channel=channel)

    def wrappable(self, thing) -> bool:
        """
        Determine if something is wrappable.
        """
        result = (
            thing is not None
            and not isinstance(
                thing,
                (bool, str, int, float, complex, list, tuple, set, dict, bytearray),
            )
            and isinstance(
                thing,
                (type, object, types.MethodType, types.FunctionType, types.ModuleType),
            )
        )
        return result

    def cleanup_exception_pre(self, ex):
        """
        When an exception is going to be recorded, this intercept allows the
        exception to be changed.  This is necessary for any exception that
        cannot be pickled.

        Common ways to deal with pickling errors here are:
          - Set one of the properties to None
          - Return a doppleganger class (looks like, smells like, but does not
            derive from the original).
        """
        return ex

    def cleanup_exception_post(self, ex):
        """
        Modify an exception during playback before it is thrown.
        """
        return ex

    def cleanup_parameters_pre(self, params):
        """
        Allows the data in the parameters (this uniquely identifies a request)
        to be modified.  This is useful in wiping out any credentials or other
        sensitive information.  When replaying in tests, if you set these bits
        to the same value, the recorded playback will match.
        """
        return params

    def cleanup_parameters_post(self, params):
        """
        Modify parameters during playback before they are hashed to locate
        a recording.  This usually does the same thing as
        cleanup_parameters_pre.
        """
        return params

    def cleanup_result_pre(self, params, result):
        """
        Some return values cannot be pickled.  This interceptor allows you to
        rewrite the result so that it can be.  Sometimes this means removing
        a property (setting it to None), sometimes it means replacing the
        result with something else entirely (a doppleganger with the same
        methods and properties as the original, but isn't derived from it).

        Common ways to deal with pickling errors here are:
          - Set one of the properties to None
          - Return a doppleganger class (looks like, smells like, but does not
            derive from the original).
        """
        return result

    def cleanup_result_post(self, result):
        """
        Modify the return value during playback before it is returned.
        """
        return result

    def clear_for_execution(self, params) -> None:
        """
        Called before any method is actually executed.  This can be used to
        implement a mechanism that ensures only certain methods are called.

        Implementations are free to raise whatever error they would like to
        identify this situation.
        """
        pass

    def _log(self, category: str, subcategory: str, msg: str) -> None:
        """
        Common funnel for interposer logs.
        """
        self.logger.log(
            self.loglevels[category][subcategory], (self.logprefix or " ") + msg
        )

    def _playback(self, params: dict) -> object:
        """
        Playback a previous recording.

        Args:
            params:  A dict containing: { method: <method called>, args: [args], kwargs: {kwargs} }

        Returns:
            Whatever object was stored
        """
        new_params = self.cleanup_parameters_post(params)
        prefix = sha1(  # nosec
            json.dumps(new_params, sort_keys=True, cls=self.encoder).encode()
        ).hexdigest()
        result_key = f"{prefix}.results"

        # Check the call order and issue a warning if warranted
        if self.call_order:
            channel = new_params.get("channel")
            if channel:
                index = self.call_order[channel].get("call_index", 0)
                calls = self.call_order[channel]["calls"]
                if len(calls) <= index:
                    raise PlaybackError("Not enough calls recorded to satisfy.")
                if new_params != calls[index]:
                    msg = f"Call {new_params} played back as call in a different order than recorded. "
                    msg += f"Call at index {index}: {self.call_order[channel]['calls'][index]}"
                    raise PlaybackError(msg)

                self.call_order[channel]["call_index"] = index + 1

            # record the call in the playback_call_order list
            if new_params["channel"] in self.playback_call_order:
                self.playback_call_order[new_params["channel"]]["calls"].append(
                    new_params
                )
            else:
                self.playback_call_order[new_params["channel"]] = {}
                self.playback_call_order[new_params["channel"]]["calls"] = [new_params]

        located = self.tape.get(result_key)
        if not located:
            raise PlaybackError(f"No calls for params {new_params} were ever recorded.")
        index = self.playback_index.get(result_key, 0)
        if len(located) <= index:
            raise PlaybackError(
                f"Call #{index} for params {new_params} was never recorded."
            )
        recorded = located[index]
        self.playback_index[result_key] = index + 1

        if self.version == 1:
            result = recorded
            exception = None
        else:
            result = recorded[0]
            exception = recorded[1]

        if exception is None:
            self._log(
                "result",
                "playback",
                f"playing back RESULT for {result_key} call #{index} "
                f"for params {new_params} hash={prefix} "
                f"type={(result.__class__.__name__ if result is not None else 'None')}: "
                f"{pformat(result)}",
            )
            return self.cleanup_result_post(result)
        else:
            self._log(
                "except",
                "playback",
                f"playing back EXCEPTION for {result_key} call #{index} "
                f"for params {new_params} hash={prefix}: {str(exception)}",
            )
            raise self.cleanup_exception_post(exception)

    def _record(self, params: dict, result: object, exception: object = None):
        """
        Records the parameters and result of an API call.

        To get the result of this recording at a later time, call playback:
            _playback(params)

        The result for each param signature are stored in a list, in the order the result is
        recorded.  Playback will replay the result in the same order as the original recording.

        Args:
            params:  A dict containing: { method: <method called>, args: [args], kwargs: {kwargs} }
            result: The result from the API call, as any python object that can be pickled
            exception: The exception that occurred as a result of the API call, if any
        """
        # Use json.dumps to turn whatever parameters we have into a string, so we can hash it
        prefix = sha1(  # nosec
            json.dumps(params, sort_keys=True, cls=self.encoder).encode()
        ).hexdigest()
        result_key = f"{prefix}.results"

        result_list = self.tape.get(result_key, [])
        result_list.append((result, exception))

        # record the call in the call_order list
        if params["channel"] in self.call_order:
            self.call_order[params["channel"]]["calls"].append(params)
        else:
            self.call_order[params["channel"]] = {}
            self.call_order[params["channel"]]["calls"] = [params]

        if exception is None:
            self._log(
                "result",
                "recorded",
                f"recording RESULT {result_key} call #{(len(result_list) - 1)} "
                f"for params {params} hash={prefix} "
                f"type={(result.__class__.__name__ if result is not None else 'None')}: "
                f"{pformat(result)}",
            )
        else:
            self._log(
                "except",
                "recorded",
                f"recording EXCEPTION {result_key} call #{(len(result_list) - 1)} "
                f"for params {params} hash={prefix}: {exception}",
            )
        self.tape[result_key] = result_list


class ScopedInterposer(Interposer, AbstractContextManager):
    """
    Allows the interposer to be used properly as a resource, since it
    handles a file.
    """

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()


class _InterposerWrapper(CallableObjectProxy):
    """
    This class is an implementation detail of Interposer.
    """

    def __init__(self, interposer: Interposer, clazz, channel="default"):
        """
        Use of self._self_... is per wrapt requirements.
        """
        super().__init__(clazz)
        self._self_channel = channel
        self._self_interposer = interposer

    def __call__(self, *args, **kwargs):
        """
        Handle a call on the wrapped item.

        This is where we inject the pre- and post- handlers.

        Calls on class definitions (initialization).
        Calls on methods and functions get recorded.
        """
        if isinstance(self.__wrapped__, (type, types.MethodType, types.FunctionType)):
            params = {
                "method": self.__wrapped__.__name__,
                "args": args,
                "kwargs": kwargs,
            }
            params[
                "channel" if self._self_interposer.version >= 3 else "context"
            ] = self._self_channel
            if self._self_interposer.mode == Mode.Playback:
                self._self_interposer.clear_for_execution(params)
                result = self._self_interposer._playback(params)
                if isinstance(self.__wrapped__, type):
                    # instantiating an object from a class definition requires
                    # us to wrap the result so that we can capture the rest of
                    # the object's usage
                    result = self._self_interposer.wrap(
                        result, channel=self._self_channel
                    )
                return result
            else:
                try:
                    self._self_interposer.clear_for_execution(params)
                    self._log(
                        "call", f"calling {self.__wrapped__} and recording result"
                    )
                    result = self._self_interposer.cleanup_result_pre(
                        params, super().__call__(*args, **kwargs)
                    )
                    params = self._self_interposer.cleanup_parameters_pre(params)
                    self._self_interposer._record(params, result)
                    if isinstance(self.__wrapped__, type):
                        # instantiating an object from a class definition requires
                        # us to wrap the result so that we can capture the rest of
                        # the object's usage
                        result = self._self_interposer.wrap(
                            result, channel=self._self_channel
                        )
                    return result
                except Exception as ex:
                    params = self._self_interposer.cleanup_parameters_pre(params)
                    ex = self._self_interposer.cleanup_exception_pre(ex)
                    self._self_interposer._record(params, None, exception=ex)
                    raise ex
        else:
            # not a class definition, method, or function
            # simply return the result of the call
            self._log("call", f"calling {self.__wrapped__} and ignoring result")
            return super().__call__(*args, **kwargs)

    def __getattr__(self, name):
        """
        Handle duck typing on the wrapped item.

        If the attribute is a function, method, class definition, wrap it so
        that when it is called, we execute the code above to capture it if
        necessary.
        """
        attr = super().__getattr__(name)
        try:
            attr = self._self_interposer.wrap(attr, channel=self._self_channel)
            # logs the positive case
        except WrappingError:
            self._log("wrap", f"NOT wrapping {self.__class__}.{name}")
            pass
        return attr

    def _log(self, subcategory: str, msg: str) -> None:
        """
        Common funnel for wrap logs.
        """
        self._self_interposer._log("wrappr", subcategory, msg)
