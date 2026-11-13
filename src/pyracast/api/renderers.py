from collections.abc import Mapping
from typing import Any

from rest_framework.renderers import JSONRenderer

from pyracast.api.public import public_source_data


class CustomerJSONRenderer(JSONRenderer):
    def render(
        self,
        data: Any,
        accepted_media_type: str | None = None,
        renderer_context: Mapping[str, Any] | None = None,
    ) -> bytes:
        context = renderer_context or {}
        request = context.get("request")
        if request is None or not request.path.startswith("/api/platform/"):
            data = public_source_data(data)
        return super().render(data, accepted_media_type, renderer_context)
