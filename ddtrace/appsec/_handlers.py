import functools
import json

import xmltodict
from wrapt.importer import when_imported

from ddtrace.appsec._iast._patch import if_iast_taint_returned_object_for, if_iast_taint_yield_tuple_for
from ddtrace.appsec._iast._utils import _is_iast_enabled
from ddtrace.contrib import trace_utils
from ddtrace.internal import core
from ddtrace.internal.constants import HTTP_REQUEST_BLOCKED
from ddtrace.internal.logger import get_logger
from ddtrace.vendor.wrapt import wrap_function_wrapper as _w

log = get_logger(__name__)
_BODY_METHODS = {"POST", "PUT", "DELETE", "PATCH"}


def _on_request_init(wrapped, instance, args, kwargs):
    wrapped(*args, **kwargs)
    if _is_iast_enabled():
        try:
            from ddtrace.appsec._iast._metrics import _set_metric_iast_instrumented_source
            from ddtrace.appsec._iast._taint_tracking import OriginType, taint_pyobject

            # TODO: instance.query_string = ??
            instance.query_string = taint_pyobject(
                pyobject=instance.query_string,
                source_name=OriginType.QUERY,
                source_value=instance.query_string,
                source_origin=OriginType.QUERY,
            )
            instance.path = taint_pyobject(
                pyobject=instance.path,
                source_name=OriginType.PATH,
                source_value=instance.path,
                source_origin=OriginType.PATH,
            )
            _set_metric_iast_instrumented_source(OriginType.PATH)
            _set_metric_iast_instrumented_source(OriginType.QUERY)
        except Exception:
            log.debug("Unexpected exception while tainting pyobject", exc_info=True)


def _on_flask_patch(flask_version):
    if _is_iast_enabled():
        try:
            from ddtrace.appsec._iast._metrics import _set_metric_iast_instrumented_source
            from ddtrace.appsec._iast._taint_tracking import OriginType

            _w(
                "werkzeug.datastructures",
                "Headers.items",
                functools.partial(if_iast_taint_yield_tuple_for, (OriginType.HEADER_NAME, OriginType.HEADER)),
            )
            _set_metric_iast_instrumented_source(OriginType.HEADER_NAME)
            _set_metric_iast_instrumented_source(OriginType.HEADER)

            _w(
                "werkzeug.datastructures",
                "ImmutableMultiDict.__getitem__",
                functools.partial(if_iast_taint_returned_object_for, OriginType.PARAMETER),
            )
            _set_metric_iast_instrumented_source(OriginType.PARAMETER)

            _w(
                "werkzeug.datastructures",
                "EnvironHeaders.__getitem__",
                functools.partial(if_iast_taint_returned_object_for, OriginType.HEADER),
            )
            _set_metric_iast_instrumented_source(OriginType.HEADER)

            _w("werkzeug.wrappers.request", "Request.__init__", _on_request_init)
            _w(
                "werkzeug.wrappers.request",
                "Request.get_data",
                functools.partial(if_iast_taint_returned_object_for, OriginType.BODY),
            )
            _set_metric_iast_instrumented_source(OriginType.BODY)

            if flask_version < (2, 0, 0):
                _w(
                    "werkzeug._internal",
                    "_DictAccessorProperty.__get__",
                    functools.partial(if_iast_taint_returned_object_for, OriginType.QUERY),
                )
                _set_metric_iast_instrumented_source(OriginType.QUERY)
        except Exception:
            log.debug("Unexpected exception while patch IAST functions", exc_info=True)


def _on_flask_blocked_request():
    core.set_item(HTTP_REQUEST_BLOCKED, True)


def _on_django_func_wrapped(fn_args, fn_kwargs, first_arg_expected_type):
    # If IAST is enabled and we're wrapping a Django view call, taint the kwargs (view's
    # path parameters)
    if _is_iast_enabled() and fn_args and isinstance(fn_args[0], first_arg_expected_type):
        from ddtrace.appsec._iast._taint_tracking import OriginType  # noqa: F401
        from ddtrace.appsec._iast._taint_tracking import is_pyobject_tainted, taint_pyobject
        from ddtrace.appsec._iast._taint_utils import LazyTaintDict

        http_req = fn_args[0]

        if not isinstance(http_req.COOKIES, LazyTaintDict):
            http_req.COOKIES = LazyTaintDict(http_req.COOKIES, origins=(OriginType.COOKIE_NAME, OriginType.COOKIE))
        if not isinstance(http_req.GET, LazyTaintDict):
            http_req.GET = LazyTaintDict(http_req.GET, origins=(OriginType.PARAMETER_NAME, OriginType.PARAMETER))
        if not isinstance(http_req.POST, LazyTaintDict):
            http_req.POST = LazyTaintDict(http_req.POST, origins=(OriginType.BODY, OriginType.BODY))
        if not is_pyobject_tainted(getattr(http_req, "_body", None)):
            http_req._body = taint_pyobject(
                http_req.body,
                source_name="body",
                source_value=http_req.body,
                source_origin=OriginType.BODY,
            )

        if not isinstance(http_req.META, LazyTaintDict):
            http_req.META = LazyTaintDict(http_req.META, origins=(OriginType.HEADER_NAME, OriginType.HEADER))
        if not isinstance(http_req.headers, LazyTaintDict):
            http_req.headers = LazyTaintDict(http_req.headers, origins=(OriginType.HEADER_NAME, OriginType.HEADER))
        http_req.path = taint_pyobject(
            http_req.path, source_name="path", source_value=http_req.path, source_origin=OriginType.PATH
        )
        http_req.path_info = taint_pyobject(
            http_req.path_info,
            source_name="path",
            source_value=http_req.path,
            source_origin=OriginType.PATH,
        )
        http_req.environ["PATH_INFO"] = taint_pyobject(
            http_req.environ["PATH_INFO"],
            source_name="path",
            source_value=http_req.path,
            source_origin=OriginType.PATH,
        )
        if fn_kwargs:
            try:
                for k, v in fn_kwargs.items():
                    fn_kwargs[k] = taint_pyobject(
                        v, source_name=k, source_value=v, source_origin=OriginType.PATH_PARAMETER
                    )
            except Exception:
                log.debug("IAST: Unexpected exception while tainting path parameters", exc_info=True)


def _on_wsgi_environ(wrapped, _instance, args, kwargs):
    if _is_iast_enabled():
        if not args:
            return wrapped(*args, **kwargs)

        from ddtrace.appsec._iast._metrics import _set_metric_iast_instrumented_source
        from ddtrace.appsec._iast._taint_tracking import OriginType  # noqa: F401
        from ddtrace.appsec._iast._taint_utils import LazyTaintDict

        _set_metric_iast_instrumented_source(OriginType.HEADER_NAME)
        _set_metric_iast_instrumented_source(OriginType.HEADER)
        # we instrument those sources on _on_django_func_wrapped
        _set_metric_iast_instrumented_source(OriginType.PATH_PARAMETER)
        _set_metric_iast_instrumented_source(OriginType.PATH)
        _set_metric_iast_instrumented_source(OriginType.COOKIE)
        _set_metric_iast_instrumented_source(OriginType.COOKIE_NAME)
        _set_metric_iast_instrumented_source(OriginType.PARAMETER)
        _set_metric_iast_instrumented_source(OriginType.PARAMETER_NAME)
        _set_metric_iast_instrumented_source(OriginType.BODY)

        return wrapped(
            *((LazyTaintDict(args[0], origins=(OriginType.HEADER_NAME, OriginType.HEADER)),) + args[1:]), **kwargs
        )

    return wrapped(*args, **kwargs)


def _on_django_patch():
    try:
        from ddtrace.appsec._iast._taint_tracking import OriginType  # noqa: F401

        when_imported("django.http.request")(
            lambda m: trace_utils.wrap(
                m,
                "QueryDict.__getitem__",
                functools.partial(if_iast_taint_returned_object_for, OriginType.PARAMETER),
            )
        )
    except Exception:
        log.debug("Unexpected exception while patch IAST functions", exc_info=True)


def _on_trace_handlers_start_response_pre(request, has_json_mixin, exception_type):
    req_body = None

    try:
        content_type = request.content_type
        if content_type == "application/json" or content_type == "text/json":
            if has_json_mixin and hasattr(request, "json") and request.json:
                req_body = request.json
            else:
                req_body = json.loads(request.data.decode("UTF-8"))
        elif content_type in ("application/xml", "text/xml"):
            req_body = xmltodict.parse(request.data)
        elif hasattr(request, "form"):
            req_body = request.form.to_dict()
    except (
        exception_type,
        AttributeError,
        RuntimeError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        xmltodict.expat.ExpatError,
        xmltodict.ParsingInterrupted,
    ):
        log.warning("Failed to parse request body", exc_info=True)
    return req_body


def listen():
    core.on("flask.request_init", _on_request_init)
    core.on("flask.blocked_request_callable", _on_flask_blocked_request)
    core.on("trace_handlers.start_response.pre", _on_trace_handlers_start_response_pre)


core.on("django.func.wrapped", _on_django_func_wrapped)
core.on("django.wsgi_environ", _on_wsgi_environ)
core.on("django.patch", _on_django_patch)
core.on("flask.patch", _on_flask_patch)
