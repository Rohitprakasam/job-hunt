"""Per-ATS details: where the form is, how to submit, what success looks like. Filling is generic."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Adapter:
    name: str
    submit_selector: str
    confirmation: re.Pattern[str]
    open_form_selector: str | None = None  # a button that reveals the form, when there is one

    def form_url(self, url: str) -> str:
        return url


class Greenhouse(Adapter):
    def form_url(self, url: str) -> str:
        # New boards show the form on the job page; old boards.greenhouse.io pages anchor it at #app.
        return url.split("#")[0]


class Lever(Adapter):
    def form_url(self, url: str) -> str:
        base = url.split("?")[0].rstrip("/")
        return base if base.endswith("/apply") else base + "/apply"


class Ashby(Adapter):
    def form_url(self, url: str) -> str:
        base = url.split("?")[0].rstrip("/")
        return base if base.endswith("/application") else base + "/application"


ADAPTERS: dict[str, Adapter] = {
    "greenhouse": Greenhouse(
        "greenhouse",
        submit_selector="button[type=submit], input[type=submit]#submit_app",
        confirmation=re.compile(r"thank you for applying|application (has been )?(submitted|received)", re.I),
        open_form_selector="button:has-text('Apply'), a:has-text('Apply for this job')",
    ),
    "lever": Lever(
        "lever",
        submit_selector="button#btn-submit, button[type=submit]",
        confirmation=re.compile(r"application submitted|thanks? for applying|we('ve| have) received", re.I),
    ),
    "ashby": Ashby(
        "ashby",
        submit_selector="button:has-text('Submit Application'), button[type=submit]",
        confirmation=re.compile(
            r"application (was )?(successfully )?submitted|thank you for (applying|your application)", re.I
        ),
    ),
}

# For tests and saved form fixtures.
GENERIC = Adapter(
    "generic",
    submit_selector="button[type=submit], input[type=submit]",
    confirmation=re.compile(r"thank you for applying|application (submitted|received)", re.I),
)
