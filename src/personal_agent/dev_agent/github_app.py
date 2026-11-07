"""The dev-agent GitHub App: private repos and scoped git access (ADR-0159 D8.1–D8.3; FRE-1685).

Only the gateway holds the App's private key, and only the gateway writes to GitHub (D8.2). The
agent container gets no GitHub credential.

Each operation mints a new installation token, scoped down to what it needs:

* repo creation: ``administration: write`` and ``metadata: read``, on the installation;
* git fetch and push: ``contents: write`` and ``metadata: read``, on one named repo.

The token reaches git only as an ``http.extraHeader`` in the environment of one git process
(:class:`GitRemote`). It is never in a URL, on a command line or on disk, and never in ``repr``.
"""

from __future__ import annotations

import base64
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

import httpx
import jwt
import structlog

from personal_agent.exceptions import DevAgentRepoError

log = structlog.get_logger(__name__)

#: A repo name of this module: a slug of at most 40 characters, a dash, and 8 hex of the job id.
REPO_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?-[0-9a-f]{8}$")
_SLUG_MAX = 40
_API_VERSION = "2022-11-28"
_HTTP_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class GitRemote:
    """A git remote and the environment that authenticates one git process to it.

    Attributes:
        url: The remote URL. It never holds a credential.
        env: Extra environment variables for the git process. They may hold a credential.
    """

    url: str
    env: Mapping[str, str] = field(default_factory=dict, repr=False)


class RepoHost(Protocol):
    """Where the dev-agent repos live. :class:`GitHubAppHost` in production."""

    async def create_private_repo(self, name: str) -> None:
        """Create the private repo ``name``, or accept it if it exists and is private."""
        ...

    async def remote(self, name: str) -> GitRemote:
        """Return git access to the repo ``name`` (fetch and push)."""
        ...

    async def is_private(self, name: str) -> bool:
        """Return whether the repo ``name`` is private."""
        ...


@dataclass(frozen=True)
class GitHubAppConfig:
    """The GitHub App of the dev agent.

    Attributes:
        app_id: The App ID.
        installation_id: The installation on the organization.
        org: The organization that holds every repo.
        private_key_pem: The App's private key (PEM).
        api_url: The REST API base URL.
        git_url: The git HTTPS base URL.
    """

    app_id: int
    installation_id: int
    org: str
    private_key_pem: str = field(repr=False)
    api_url: str = "https://api.github.com"
    git_url: str = "https://github.com"

    @classmethod
    def from_base64_key(
        cls, *, app_id: int, installation_id: int, org: str, private_key_b64: str, api_url: str
    ) -> GitHubAppConfig:
        """Build the config from a base64-encoded PEM key (the form of the ``.env`` value).

        Args:
            app_id: The App ID.
            installation_id: The installation ID.
            org: The organization.
            private_key_b64: The PEM file, base64-encoded.
            api_url: The REST API base URL.

        Returns:
            The config.

        Raises:
            ValueError: If the value does not decode to a PEM private key.
        """
        pem = base64.b64decode(private_key_b64.strip(), validate=True).decode("ascii")
        if "PRIVATE KEY-----" not in pem:
            raise ValueError("the GitHub App key does not decode to a PEM private key")
        return cls(
            app_id=app_id,
            installation_id=installation_id,
            org=org,
            private_key_pem=pem,
            api_url=api_url,
        )


def repo_name(slug: str | None, job_id: UUID) -> str:
    """Return the repo name of a job: ``<slug>-<first 8 hex of the job id>`` (D8.3).

    Args:
        slug: The page slug, or ``None`` for a build job.
        job_id: The job.

    Returns:
        A name that matches :data:`REPO_NAME`. A slug with no usable character becomes ``page``.
    """
    cleaned = re.sub(r"[^a-z0-9]+", "-", (slug or "").lower()).strip("-")
    cleaned = cleaned[:_SLUG_MAX].strip("-") or "page"
    return f"{cleaned}-{job_id.hex[:8]}"


def app_jwt(config: GitHubAppConfig, *, now: float | None = None) -> str:
    """Return a JWT that authenticates as the App, valid for 9 minutes.

    ``iat`` is 60 seconds in the past, as GitHub recommends against clock drift.

    Args:
        config: The App.
        now: The current Unix time. Defaults to the system clock.

    Returns:
        The RS256-signed token.
    """
    issued = int(now if now is not None else time.time())
    claims = {"iss": str(config.app_id), "iat": issued - 60, "exp": issued + 540}
    return jwt.encode(claims, config.private_key_pem, algorithm="RS256")


def _check_name(name: str) -> None:
    if not REPO_NAME.match(name):
        raise ValueError(f"{name!r} is not a dev-agent repo name")


class GitHubAppHost:
    """The dev-agent repos in the App's organization."""

    def __init__(self, config: GitHubAppConfig, client: httpx.AsyncClient) -> None:
        """Create the host.

        Args:
            config: The App.
            client: The HTTP client. The caller closes it.
        """
        self._config = config
        self._client = client

    def _headers(self, bearer: str) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {bearer}",
            "X-GitHub-Api-Version": _API_VERSION,
            "User-Agent": "seshat-dev-agent",
        }

    async def _request(
        self, method: str, path: str, bearer: str, body: Mapping[str, object] | None = None
    ) -> httpx.Response:
        try:
            return await self._client.request(
                method,
                f"{self._config.api_url}{path}",
                headers=self._headers(bearer),
                json=body,
                timeout=_HTTP_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            raise DevAgentRepoError(f"GitHub {method} {path}: {type(exc).__name__}") from None

    @staticmethod
    def _fail(response: httpx.Response, what: str) -> DevAgentRepoError:
        # The body is never quoted: an error page could echo a request header.
        return DevAgentRepoError(f"GitHub {what} failed with HTTP {response.status_code}")

    async def _token(
        self, permissions: Mapping[str, str], repositories: list[str] | None = None
    ) -> str:
        body: dict[str, object] = {"permissions": dict(permissions)}
        if repositories is not None:
            body = {"repositories": repositories, **body}
        response = await self._request(
            "POST",
            f"/app/installations/{self._config.installation_id}/access_tokens",
            app_jwt(self._config),
            body,
        )
        if response.status_code != 201:
            raise self._fail(response, "installation token")
        token = response.json().get("token")
        if not isinstance(token, str) or not token:
            raise DevAgentRepoError("GitHub returned no installation token")
        return token

    def _private_of_org(self, body: object) -> bool:
        if not isinstance(body, dict):
            return False
        owner = body.get("owner")
        login = owner.get("login") if isinstance(owner, dict) else None
        return (
            body.get("private") is True
            and isinstance(login, str)
            and login.lower() == self._config.org.lower()
        )

    async def create_private_repo(self, name: str) -> None:
        """Create the private repo ``name`` in the organization (D8.2).

        A repo of that name that exists already is accepted only if it is private and in the
        organization. A retry of the same job therefore finds its own repo.

        Args:
            name: The repo name. It must match :data:`REPO_NAME`.

        Raises:
            ValueError: If the name is not a dev-agent repo name. No call was made.
            DevAgentRepoError: If a call fails, or the repo is not private.
        """
        _check_name(name)
        token = await self._token({"administration": "write", "metadata": "read"})
        response = await self._request(
            "POST",
            f"/orgs/{self._config.org}/repos",
            token,
            {
                "name": name,
                "private": True,
                "auto_init": False,
                "has_issues": False,
                "has_projects": False,
                "has_wiki": False,
            },
        )
        if response.status_code == 201:
            body = response.json()
        elif response.status_code == 422:
            existing = await self._request("GET", f"/repos/{self._config.org}/{name}", token)
            if existing.status_code != 200:
                raise self._fail(existing, "repo read")
            body = existing.json()
        else:
            raise self._fail(response, "repo creation")
        if not self._private_of_org(body):
            raise DevAgentRepoError(f"repo {name} is not private in {self._config.org}")
        log.info(
            "dev_agent_repo_ready",
            repo=name,
            created=response.status_code == 201,
            trace_id=None,
        )

    async def is_private(self, name: str) -> bool:
        """Return whether the repo is private, as the GitHub API reports it.

        Args:
            name: The repo name.

        Returns:
            ``True`` only for a private repo of the organization.

        Raises:
            DevAgentRepoError: If the read fails.
        """
        _check_name(name)
        token = await self._token({"metadata": "read"}, [name])
        response = await self._request("GET", f"/repos/{self._config.org}/{name}", token)
        if response.status_code != 200:
            raise self._fail(response, "repo read")
        return self._private_of_org(response.json())

    async def remote(self, name: str) -> GitRemote:
        """Return git access to one repo, with a token scoped to it.

        Args:
            name: The repo name.

        Returns:
            The HTTPS remote and the environment that carries the token as a header.

        Raises:
            DevAgentRepoError: If the token cannot be minted.
        """
        _check_name(name)
        token = await self._token({"contents": "write", "metadata": "read"}, [name])
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        return GitRemote(
            url=f"{self._config.git_url}/{self._config.org}/{name}.git",
            env={
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": f"http.{self._config.git_url}/.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
            },
        )
