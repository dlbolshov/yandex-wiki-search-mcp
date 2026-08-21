import asyncio
import contextlib
import errno
import json
import logging
import os
import random
import secrets
import stat
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any, BinaryIO, Literal, TypeVar

from aiohttp import (
    ClientConnectionError,
    ClientError,
    ClientPayloadError,
    ClientResponse,
    ClientSession,
    ClientTimeout,
    TraceConfig,
    TraceRequestEndParams,
    TraceRequestExceptionParams,
    TraceRequestStartParams,
)

from mcp_wiki.wiki.custom.anchors import append_content_to_anchor_source
from mcp_wiki.wiki.custom.errors import (
    AttachmentNotFound,
    GridNotFound,
    PageNotFound,
    ResponseTooLarge,
    WikiApiError,
    WikiConfigError,
    WikiError,
    WikiLocalFileError,
    WikiOperationError,
    WikiTransportError,
    build_api_error,
)
from mcp_wiki.wiki.custom.mimes import IMAGE_MAGIC_PREFIX_BYTES, image_mime
from mcp_wiki.wiki.custom.slugs import normalize_slug
from mcp_wiki.wiki.proto.common import YandexAuth, select_org
from mcp_wiki.wiki.proto.pages import WikiProtocol, validate_page_update_args
from mcp_wiki.wiki.proto.types.pages import (
    AttachmentContent,
    AttachmentDeleteResponse,
    AttachmentDownloadResult,
    AttachmentListResponse,
    AttachmentResultsResponse,
    ClonedPageRef,
    CommentsResponse,
    DeleteCommentResponse,
    DeletePageResponse,
    DescendantsResponse,
    GridCellsResponse,
    GridCreateRequest,
    GridDeleteResponse,
    GridMutationResponse,
    GridOperationResponse,
    GridsResponse,
    GridUpdateRequest,
    GridUpdateResponse,
    PageCloneStatus,
    PageComment,
    RecoverPageResponse,
    ResourcesResponse,
    SearchAuthor,
    SearchDateInterval,
    SearchResponse,
    UploadAttachmentResult,
    UploadLocation,
    UploadSessionResponse,
    WikiCurrentUser,
    WikiGrid,
    WikiPage,
)

SEARCH_LIMIT_MAX = 50

# Page clone is a deferred operation. Observed to finish on the first poll
# (~1s, 2026-08-08); the timeout is a guard against an operation that never
# settles, not an expected wait.
CLONE_POLL_INTERVAL = 0.5
CLONE_POLL_TIMEOUT = 30.0
CLONE_FAILED_STATUSES = frozenset({"failed", "error", "cancelled"})

RETRY_STATUSES = frozenset({429, 502, 503, 504})
RETRY_BASE_DELAY = 0.3
RETRY_AFTER_MAX = 3.0

# Ceiling for an error body on a request that set max_bytes or a sink. Generous
# for a JSON error envelope, and unlike the success ceiling it truncates instead
# of raising — the point is to keep the API's own message, not to police it.
ERROR_BODY_MAX_BYTES = 65_536

# Chunk size for streaming a download to disk. Deliberately not CHUNK_SIZE
# (the 5 MiB upload part size, which the upload API dictates): this one only
# bounds how much of the body is resident between two file writes. 1 MiB
# because every chunk costs one thread-pool hop (~87 us measured) against a
# ~19 us write — at 64 KiB that was 1600 hops and ~2x the wall time for a
# 100 MB file. (The hops now run on a per-call worker rather than the
# shared default executor, so they no longer queue behind uploads; the
# per-hop cost is what still argues for the larger chunk.)
DOWNLOAD_CHUNK_SIZE = 1_048_576

# A streamed download has no total timeout: it is the one call whose duration
# is the file's size divided by the link speed, and a 30 s session-wide total
# (which aiohttp applies to the body read, not just the handshake) made every
# large attachment permanently unfetchable. A stall detector is the right
# shape instead — it fires when the peer stops sending, never because the file
# is big.
DOWNLOAD_STALL_TIMEOUT = ClientTimeout(sock_connect=30, sock_read=60)


logger = logging.getLogger(__name__)

_T = TypeVar("_T")


def _open_binary(path: Path) -> BinaryIO:
    return path.open("rb")


# `.part` name budget: the random tag plus the two dots plus the suffix.
_PART_SUFFIX_BUDGET = len(".") + 8 + len(".part")
_NAME_MAX = 255
_PART_ATTEMPTS = 4
_WINDOWS_MAX_PATH = 260


def local_path(path: str) -> Path:
    """Normalize a caller-supplied local path.

    Shared by both filesystem tools so `~/report.pdf` means the same thing to
    upload and to download; it used to expand on one side only.

    Sync on purpose: expanduser only reads environment state, but pathlib
    methods inside an async function trip ASYNC240 — and the rule is right
    in general, so it is not silenced there.
    """
    return Path(path).expanduser()


def _parent_is_not_a_directory(target: Path) -> bool:
    """Is some existing ancestor of `target` a non-directory?

    Only the nearest existing ancestor matters: everything below it is what
    `mkdir(parents=True)` would create, and it can only create under a real
    directory.
    """
    for parent in target.parents:
        try:
            st = parent.stat()
        except OSError:
            continue
        return not stat.S_ISDIR(st.st_mode)
    return False  # pragma: no cover - the root always exists and is a directory


def _probe_target(target: Path, *, overwrite: bool) -> None:
    """Reject an unusable target before a byte is transferred.

    Advisory only: the same conditions are re-checked in `_open_part`, where
    the file is actually created and the answer is authoritative. This exists
    so that "you pointed at a directory" costs nothing instead of arriving
    after a multi-megabyte download.
    """
    try:
        existing = target.stat()
    except FileNotFoundError:
        # Windows reports a regular file in the middle of a path as
        # ERROR_PATH_NOT_FOUND, which CPython maps to ENOENT rather than
        # ENOTDIR — so "missing" and "blocked by a file" arrive identically
        # there and the parent has to be asked directly.
        if _parent_is_not_a_directory(target):  # pragma: no cover - Windows only
            # POSIX raises NotADirectoryError below instead, so this arm is
            # the other half of the matrix.
            raise WikiLocalFileError(
                f"Cannot use {target}: a parent path component is a file"
            ) from None
        return
    except NotADirectoryError as exc:
        # POSIX says so outright; no transfer can ever land here.
        raise WikiLocalFileError(
            f"Cannot use {target}: a parent path component is a file", cause=exc
        ) from exc
    except OSError:
        return  # _open_part will produce the real diagnosis
    if stat.S_ISDIR(existing.st_mode):
        raise WikiLocalFileError(
            f"{target} is a directory, not a file. Give save_to a full file "
            "path, including the file name."
        )
    if not overwrite:
        raise WikiLocalFileError(
            f"File already exists: {target}. Pass overwrite=true to replace it."
        )


def _write_all(fd: int, chunk: bytes) -> None:
    """os.write may write less than asked; loop until the chunk is out."""
    view = memoryview(chunk)
    while view:
        view = view[os.write(fd, view) :]


def _part_path(target: Path) -> Path:
    """A sibling `<name>.<tag>.part` that still fits in NAME_MAX.

    The whole target name would be the natural prefix, but it can already be
    close to the 255-byte limit, and the tag plus suffix add 14 more — an
    attachment's own filename is the obvious thing for a caller to reuse, so
    this is reachable with a perfectly legal target name. Truncation is on a
    UTF-8 character boundary, since names are bytes to the kernel but text here.
    """
    budget = _NAME_MAX - _PART_SUFFIX_BUDGET
    if os.name == "nt":
        # Windows also caps the whole path at MAX_PATH (260) unless the machine
        # opted into long paths, and the suffix pushes a target Windows itself
        # accepts over the line. Shrink the prefix by whatever the parent, the
        # separator and the suffix spend; a name too short to keep is replaced
        # wholesale.
        budget = min(  # pragma: no cover - the other half of the matrix
            budget,
            _WINDOWS_MAX_PATH - len(str(target.parent)) - 1 - _PART_SUFFIX_BUDGET,
        )
    stem = target.name.encode()[: max(budget, 0)]
    prefix = stem.decode(errors="ignore") or "download"
    return target.with_name(f"{prefix}.{secrets.token_hex(4)}.part")


def _open_part(target: Path, *, overwrite: bool) -> tuple[int, Path]:
    """Create the `.part` file beside `target` and return its fd.

    Deliberately not `tempfile.mkstemp`: that hardcodes mode 0600 in defiance
    of the umask, and `os.replace` moves the inode, so those permissions would
    become the delivered file's — every download owner-only, and an overwrite
    silently discarding the replaced file's mode and group. `os.open` with
    0o666 lets the kernel apply the umask instead, which is exactly what a
    plain `open(path, "wb")` (or curl, or wget) produces: 0644 under the usual
    umask, and never executable, since 0o666 carries no execute bit. When a
    file is being replaced its own mode wins, because writing over a file does
    not change its permissions.
    """
    inherited: int | None = None
    try:
        existing = target.stat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise WikiLocalFileError(f"Cannot inspect {target}", cause=exc) from exc
    else:
        if stat.S_ISDIR(existing.st_mode):
            raise WikiLocalFileError(
                f"{target} is a directory, not a file. Give save_to a full "
                "file path, including the file name."
            )
        if not overwrite:
            raise WikiLocalFileError(
                f"File already exists: {target}. Pass overwrite=true to replace it."
            )
        inherited = stat.S_IMODE(existing.st_mode)

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WikiLocalFileError(
            f"Cannot create the directory for {target}", cause=exc
        ) from exc

    for _ in range(_PART_ATTEMPTS):
        candidate = _part_path(target)
        try:
            # O_BINARY or Windows opens this in the CRT's text mode, where
            # every \n written becomes \r\n and the return value of write()
            # does not say so — silently corrupting every binary attachment.
            # The stdlib treats the flag as mandatory for raw os.open byte
            # I/O (_pyio, tempfile, tarfile, fileinput all OR it in); the
            # tempfile this replaced included it via _bin_openflags.
            fd = os.open(
                candidate,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0),
                0o666,
            )
        except FileExistsError:  # pragma: no cover - 8 random bytes collided
            continue
        except OSError as exc:
            raise WikiLocalFileError(
                f"Cannot write beside {target}", cause=exc
            ) from exc
        if inherited is not None and os.name == "posix":  # pragma: no branch
            # POSIX-only twice over: os.fchmod exists on Windows only since
            # 3.13, and chmod there toggles nothing but the read-only
            # attribute — there is no mode worth inheriting.
            try:
                os.fchmod(fd, inherited)
            except OSError as exc:
                # vfat/exFAT and many FUSE and SMB mounts answer EPERM here —
                # the same filesystems _place_part's fallback exists for. The
                # fd and the file are already ours, so clean up rather than
                # letting a raw OSError escape past the ownership handshake.
                _discard_part(fd, candidate)
                raise WikiLocalFileError(
                    f"Cannot set permissions on {target}", cause=exc
                ) from exc
        return fd, candidate
    raise WikiLocalFileError(  # pragma: no cover - 8 random bytes, four tries
        f"Could not create a temporary file beside {target}"
    )


# link(2) failures that mean "this filesystem cannot hardlink at all"
# (FAT/exFAT sticks, some SMB/NFS mounts), as opposed to a real problem with
# these specific paths. EACCES is included deliberately: were it a genuine
# permission problem, the replace fallback fails with the same errno — now
# wrapped with the target's name instead of escaping bare.
_HARDLINK_UNSUPPORTED_ERRNOS = frozenset(
    {
        errno.EPERM,
        errno.EOPNOTSUPP,
        errno.ENOSYS,
        errno.EXDEV,
        errno.EINVAL,
        errno.EACCES,
    }
)


def _refuse_existing(target: Path, cause: OSError) -> WikiLocalFileError:
    """The one wording for "the name is taken", shared with _probe_target."""
    return WikiLocalFileError(
        f"File already exists: {target}. Pass overwrite=true to replace it.",
        cause=cause,
    )


def _fsync_directory(path: Path) -> None:
    """Make a completed rename durable — where that is possible at all.

    POSIX-only by necessity, not caution: on Windows `os.open` cannot open a
    directory (the CRT answers EACCES for any directory), and FlushFileBuffers
    does not accept directory handles anyway, so there is no equivalent to
    reach for — NTFS journals the rename on its own schedule.
    """
    if os.name != "posix":  # pragma: no cover - the other half of the matrix
        return
    dir_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _place_part(part: Path, target: Path, *, overwrite: bool) -> None:
    """Put the finished `.part` under the final name.

    Without ``overwrite`` the link/unlink pair replaces ``os.replace``: `link`
    fails with EEXIST if the name was taken while the transfer ran, so the
    refusal is the kernel's, at the instant of creation. `os.replace` would
    clobber whatever appeared during those minutes despite ``overwrite=false``.

    Filesystems that cannot hardlink fall back to a fresh existence check plus
    rename: a narrow race window reopens there, but the alternative is failing
    every non-overwrite download to a FAT stick or a network mount after the
    whole transfer already ran.
    """
    if overwrite:
        part.replace(target)
        return
    try:
        os.link(part, target)
    except FileExistsError as exc:
        raise _refuse_existing(target, exc) from exc
    except OSError as exc:
        if exc.errno not in _HARDLINK_UNSUPPORTED_ERRNOS:
            raise
        if target.exists():
            raise _refuse_existing(target, exc) from exc
        part.replace(target)
        return
    part.unlink()


def _commit_part(fd: int, part: Path, target: Path, *, overwrite: bool) -> None:
    """Flush the `.part` to storage and put it in place under the final name.

    fsync before the rename is the half that makes the write durable, not just
    atomic: `close()` only hands the buffers to the page cache, so without it a
    crash can leave the rename committed while the data blocks are not — a
    full-length file of zeros under the final name, which is precisely the
    "crashed download masquerading as a finished one" this is supposed to
    prevent. fsync is also where a full disk actually reports itself: with
    delayed allocation ENOSPC arrives here, not at write().

    Owns the fd and the `.part` from the moment it is entered: every failure
    is cleaned up and wrapped *here*, and the caller must not touch either
    again — a second os.close on the same number could tear down an fd the
    pool has already handed to someone else.
    """
    try:
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        _place_part(part, target, overwrite=overwrite)
    except WikiLocalFileError:
        with contextlib.suppress(OSError):
            part.unlink()
        raise
    except OSError as exc:
        with contextlib.suppress(OSError):
            part.unlink()
        raise WikiLocalFileError(f"Cannot finish writing {target}", cause=exc) from exc
    # Past the rename the bytes are durable in the file and the entry exists;
    # only the entry's own durability is still in question. Raising here would
    # tell the caller to retry a download that is already complete and correct
    # — and, with overwrite, one that has already replaced their file.
    try:
        _fsync_directory(target.parent)
    except OSError as exc:
        logger.warning(
            "%s is in place but its directory entry was not flushed: %s", target, exc
        )


def _discard_part(fd: int, part: Path) -> None:
    """Drop a failed transfer's `.part`, tolerating a half-torn-down state.

    Deliberately free of awaits: this runs from an `except BaseException`, so a
    second cancellation delivered at an await here would skip the unlink and
    leave the partial file in the caller's directory.
    """
    with contextlib.suppress(OSError):
        os.close(fd)
    with contextlib.suppress(OSError):
        part.unlink()


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with equal jitter: [0.5x, 1.0x] of the nominal delay."""
    nominal = RETRY_BASE_DELAY * 2 ** (attempt - 1)
    return nominal * (0.5 + random.random() * 0.5)  # noqa: S311


def _retry_delay(attempt: int, retry_after: str | None) -> float | None:
    """Delay before the next attempt, or None when the request must not be retried.

    A ``Retry-After`` header wins over the backoff, but only within
    ``RETRY_AFTER_MAX`` — when the server asks to wait longer, the caller gets the
    error right away instead of a tool call hanging on a sleep.
    """
    if retry_after is not None:
        try:
            requested = float(retry_after)
        except ValueError:
            # HTTP-date form: fall back to the regular backoff.
            return _backoff_delay(attempt)
        if not requested <= RETRY_AFTER_MAX:  # "not <=" so that nan also fails fast
            return None
        return max(requested, 0.0)
    return _backoff_delay(attempt)


def _build_trace_config() -> TraceConfig:
    async def on_request_start(
        _session: ClientSession,
        ctx: SimpleNamespace,
        _params: TraceRequestStartParams,
    ) -> None:
        ctx.start_time = asyncio.get_running_loop().time()

    async def on_request_end(
        _session: ClientSession,
        ctx: SimpleNamespace,
        params: TraceRequestEndParams,
    ) -> None:
        elapsed_ms = (asyncio.get_running_loop().time() - ctx.start_time) * 1000
        logger.debug(
            "%s %s -> %s (%.0f ms)",
            params.method,
            params.url.path,
            params.response.status,
            elapsed_ms,
        )

    async def on_request_exception(
        _session: ClientSession,
        ctx: SimpleNamespace,
        params: TraceRequestExceptionParams,
    ) -> None:
        elapsed_ms = (asyncio.get_running_loop().time() - ctx.start_time) * 1000
        logger.debug(
            "%s %s -> %r (%.0f ms)",
            params.method,
            params.url.path,
            params.exception,
            elapsed_ms,
        )

    trace_config = TraceConfig()
    trace_config.on_request_start.append(on_request_start)
    trace_config.on_request_end.append(on_request_end)
    trace_config.on_request_exception.append(on_request_exception)
    return trace_config


class WikiClient(WikiProtocol):
    CHUNK_SIZE = 5 * 1024 * 1024

    def __init__(
        self,
        *,
        token: str | None,
        iam_token: str | None = None,
        auth_scheme: Literal["OAuth", "Bearer"] = "OAuth",
        org_id: str | None = None,
        cloud_org_id: str | None = None,
        base_url: str = "https://api.wiki.yandex.net",
        timeout: float = 30,
        upload_timeout: float = 300,
        max_retries: int = 2,
    ):
        self._token = token
        self._iam_token = iam_token
        self._auth_scheme = auth_scheme
        self._org_id = org_id
        self._cloud_org_id = cloud_org_id
        self._base_url = base_url
        self._timeout = ClientTimeout(total=timeout)
        self._upload_timeout = ClientTimeout(total=upload_timeout)
        self._max_retries = max(max_retries, 0)
        self._session: ClientSession | None = None

    async def prepare(self) -> None:
        if self._session is None or self._session.closed:
            self._session = ClientSession(
                base_url=self._base_url,
                timeout=self._timeout,
                trace_configs=[_build_trace_config()],
            )

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> "WikiClient":
        await self.prepare()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    @property
    def _http(self) -> ClientSession:
        if self._session is None:
            raise RuntimeError(
                "WikiClient is not prepared. "
                "Call prepare() or use 'async with WikiClient(...)'."
            )
        return self._session

    def _build_headers(self, auth: YandexAuth | None = None) -> dict[str, str]:
        if auth and auth.token:
            auth_header = f"{self._auth_scheme} {auth.token}"
        elif self._token:
            auth_header = f"{self._auth_scheme} {self._token}"
        elif self._iam_token:
            auth_header = f"Bearer {self._iam_token}"
        else:
            raise WikiConfigError(
                "No authentication method provided. Configure wiki_token, wiki_iam_token, or OAuth."
            )

        org_id, cloud_org_id = select_org(
            auth,
            default_org_id=self._org_id,
            default_cloud_org_id=self._cloud_org_id,
        )

        if org_id and cloud_org_id:
            raise WikiConfigError(
                "Only one of org_id or cloud_org_id should be provided."
            )
        if not org_id and not cloud_org_id:
            raise WikiConfigError(
                "No organization for this request. Set WIKI_ORG_ID (or "
                "WIKI_CLOUD_ORG_ID) on the server, or — under OAuth, where the "
                "organization travels per request — append ?orgId=... (or "
                "?cloudOrgId=...) to the MCP server URL."
            )

        headers = {"Authorization": auth_header}
        if org_id:
            headers["X-Org-Id"] = org_id
        if cloud_org_id:
            headers["X-Cloud-Org-Id"] = cloud_org_id
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        auth: YandexAuth | None = None,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        data: Any = None,
        content_type: str | None = None,
        not_found: Callable[[], WikiError] | None = None,
        timeout: ClientTimeout | None = None,  # noqa: ASYNC109
        retryable: bool | None = None,
    ) -> bytes:
        """Perform a Wiki API request and return the response body.

        The thin common case of `_request_raw`, for the callers — all but the
        attachment endpoints — that have no use for the Content-Type header,
        the per-response ceiling or the streaming sink.
        """
        reply = await self._request_raw(
            method,
            path,
            auth=auth,
            params=params,
            json_body=json_body,
            data=data,
            content_type=content_type,
            not_found=not_found,
            timeout=timeout,
            retryable=retryable,
        )
        return reply.content

    async def _request_raw(
        self,
        method: str,
        path: str,
        *,
        auth: YandexAuth | None = None,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        data: Any = None,
        content_type: str | None = None,
        not_found: Callable[[], WikiError] | None = None,
        timeout: ClientTimeout | None = None,  # noqa: ASYNC109
        retryable: bool | None = None,
        max_bytes: Callable[[str | None], int] | None = None,
        body_sink: Callable[[bytes], Awaitable[None]] | None = None,
    ) -> AttachmentContent:
        """Perform a Wiki API request; the body travels with its Content-Type.

        ``retryable`` marks the call as safe to repeat; it defaults to GET. Only such
        calls are retried, because a 5xx may well arrive after the write has been
        applied — repeating a page creation or an append would duplicate content.

        ``max_bytes`` caps the response body. Every other endpoint answers with
        JSON the API itself keeps small, but attachment download streams whatever
        was uploaded, so the ceiling has to live here rather than above: by the
        time a caller can measure ``len(payload)`` the bytes are already resident,
        which on a shared OAuth deployment is a per-caller memory hole. Enforced
        twice — against ``Content-Length`` before reading anything, and against the
        stream itself for a chunked response that declares no length. A callable
        picks the ceiling per response, from its Content-Type: the download
        endpoint serves images and text through one URL, and which ceiling is
        right is only known once the header arrives (always before the body).

        ``body_sink`` diverts a success body out of memory: chunks go to the
        sink as they arrive and the returned body is empty. Error bodies still
        materialize (truncated) — they are diagnostics, not payload. A sink
        call must not be retried: chunks already delivered cannot be unsent,
        so a second attempt would duplicate them.
        """
        headers = self._build_headers(auth)
        if content_type:
            headers["Content-Type"] = content_type

        kwargs: dict[str, Any] = {"headers": headers}
        if params is not None:
            kwargs["params"] = params
        if json_body is not None:
            kwargs["json"] = json_body
        if data is not None:
            kwargs["data"] = data
        if timeout is not None:
            kwargs["timeout"] = timeout

        # One predicate instead of three restatements below.
        diverted = max_bytes is not None or body_sink is not None
        can_retry = method == "GET" if retryable is None else retryable
        if body_sink is not None and can_retry:
            raise ValueError("body_sink requires retryable=False")
        attempts = self._max_retries + 1 if can_retry else 1

        for attempt in range(1, attempts + 1):
            try:
                async with self._http.request(method, path, **kwargs) as response:
                    status = response.status
                    retry_after = response.headers.get("Retry-After")
                    # aiohttp already parsed the header (params stripped,
                    # lowercased); re-splitting it in the tool layer only
                    # invited the two copies to drift. The explicit None
                    # preserves "the server said nothing", which aiohttp
                    # otherwise reports as application/octet-stream.
                    reply_type = (
                        response.content_type
                        if response.headers.get("Content-Type")
                        else None
                    )
                    if status >= 400:
                        # An error body is a diagnostic, not the payload the
                        # caller asked for, so on a capped or sunk request it
                        # is truncated rather than refused or diverted:
                        # raising ResponseTooLarge here would replace the
                        # API's own explanation of what went wrong with a
                        # complaint about its size. build_api_error already
                        # degrades to a bare status when the JSON does not
                        # parse, which is what a truncated envelope looks like.
                        payload = (
                            await self._read_truncated(response, ERROR_BODY_MAX_BYTES)
                            if diverted
                            else await response.read()
                        )
                    elif body_sink is not None:
                        async for chunk in response.content.iter_chunked(
                            DOWNLOAD_CHUNK_SIZE
                        ):
                            await body_sink(chunk)
                        payload = b""
                    elif max_bytes is None:
                        payload = await response.read()
                    else:
                        payload = await self._read_capped(
                            response, method, path, max_bytes(reply_type)
                        )
            except (ClientError, TimeoutError) as exc:
                # A plain total timeout raises bare TimeoutError, which is not a
                # ClientError; ServerTimeoutError is both. Timeouts are never
                # retried, and only connection/payload failures ever were.
                retryable_failure = isinstance(
                    exc, ClientConnectionError | ClientPayloadError
                ) and not isinstance(exc, TimeoutError)
                if not retryable_failure or attempt == attempts:
                    raise WikiTransportError(method, path, exc) from exc
                delay = _backoff_delay(attempt)
                logger.warning(
                    "%s %s failed with %r, retrying in %.2fs (attempt %d/%d)",
                    method,
                    path,
                    exc,
                    delay,
                    attempt + 1,
                    attempts,
                )
                await asyncio.sleep(delay)
                continue

            if status == 404 and not_found is not None:
                raise not_found()
            if status in RETRY_STATUSES and attempt < attempts:
                status_delay = _retry_delay(attempt, retry_after)
                if status_delay is not None:
                    logger.warning(
                        "%s %s returned %d, retrying in %.2fs (attempt %d/%d)",
                        method,
                        path,
                        status,
                        status_delay,
                        attempt + 1,
                        attempts,
                    )
                    await asyncio.sleep(status_delay)
                    continue
            if status >= 400:
                raise build_api_error(status, payload)
            return AttachmentContent(payload, reply_type)

        # The loop always returns or raises; this only keeps the function's
        # return type honest if that ever stops being true.
        raise RuntimeError(  # pragma: no cover
            "unreachable: request loop exited without a result"
        )

    @staticmethod
    async def _drain(response: ClientResponse, limit: int) -> bytes:
        """Read at most ``limit`` bytes off the body, stopping at EOF.

        The one place bytes are pulled from a response. `iter_chunked` is what
        makes that safe: `StreamReader.read(n)` returns whatever happens to be
        buffered, up to `n` — not `n` bytes — so the hand-rolled loops this
        replaces each had to re-derive the same "keep going until EOF" rule,
        and differed by an off-by-one nobody could see was deliberate.

        Returning `limit + 1` bytes is the signal that the body was longer;
        callers decide whether that is an error or just a place to stop.
        """
        body = bytearray()
        async for chunk in response.content.iter_chunked(DOWNLOAD_CHUNK_SIZE):
            body.extend(chunk)
            if len(body) > limit:
                del body[limit + 1 :]
                break
        return bytes(body)

    @classmethod
    async def _read_capped(
        cls, response: ClientResponse, method: str, path: str, max_bytes: int
    ) -> bytes:
        """Read a body, refusing to materialize more than ``max_bytes``.

        ``Content-Length`` settles it without reading a byte when the server
        sends one. Otherwise the stream itself decides: overflow raises inside
        the caller's ``async with``, which leaves the response undrained, so
        aiohttp closes the connection instead of pulling the rest down.
        """
        declared = response.content_length
        if declared is not None and declared > max_bytes:
            raise ResponseTooLarge(method, path, declared, max_bytes)
        body = await cls._drain(response, max_bytes)
        if len(body) > max_bytes:
            raise ResponseTooLarge(method, path, None, max_bytes)
        return body

    @classmethod
    async def _read_truncated(cls, response: ClientResponse, limit: int) -> bytes:
        """Read at most ``limit`` bytes, stopping quietly rather than raising.

        Overflow is not an error here: the caller wants whatever the body says,
        and a body this large is malformed rather than forbidden.
        """
        return (await cls._drain(response, limit))[:limit]

    @staticmethod
    def _json_or_empty(payload: bytes) -> Any:
        if not payload:
            return {}
        return json.loads(payload)

    async def page_get_by_slug(
        self,
        slug: str,
        *,
        fields: list[str] | None = None,
        auth: YandexAuth | None = None,
    ) -> WikiPage:
        normalized_slug = normalize_slug(slug)
        params: dict[str, Any] = {"slug": normalized_slug}
        if fields:
            params["fields"] = ",".join(fields)

        payload = await self._request(
            "GET",
            "v1/pages",
            params=params,
            auth=auth,
            not_found=lambda: PageNotFound(normalized_slug),
        )
        return WikiPage.model_validate_json(payload)

    async def page_get(
        self,
        page_id: int,
        *,
        fields: list[str] | None = None,
        auth: YandexAuth | None = None,
    ) -> WikiPage:
        params: dict[str, Any] = {}
        if fields:
            params["fields"] = ",".join(fields)

        payload = await self._request(
            "GET",
            f"v1/pages/{page_id}",
            params=params if params else None,
            auth=auth,
            not_found=lambda: PageNotFound(page_id),
        )
        return WikiPage.model_validate_json(payload)

    async def page_search(
        self,
        query: str,
        *,
        limit: int = 10,
        cluster: str | None = None,
        result_type: str | None = None,
        authors: list[SearchAuthor] | None = None,
        created_at: SearchDateInterval | None = None,
        modified_at: SearchDateInterval | None = None,
        highlight: bool = False,
        auth: YandexAuth | None = None,
    ) -> SearchResponse:
        # The search endpoint reads "limit" from the POST body and silently
        # ignores "page_size" (always returning 10); limit > 50 is a 400.
        # Verified against the live API 2026-08-02. Named limit rather than
        # page_size on purpose: there is no pagination behind it — the
        # endpoint's cursors are always null. The documented "cursor" and
        # "order_by" are dead on the wire (2026-08-11) and stay unexposed.
        # Filters run server-side, before the limit (verified 2026-08-11):
        # "cluster" takes deep slug prefixes, an unknown one is 200 with no
        # results, and the date intervals require both bounds — the model
        # enforces that so the wire error never happens. "authors" entries
        # OR together and match the page owner; an unknown identity is 200
        # with no results, and an empty list is the same as no filter, so
        # it is dropped here rather than sent (verified 2026-08-18).
        # The documented "show_obsolete" is dead on the wire — obsolete
        # pages come back regardless (verified 2026-08-18) — and stays
        # unexposed alongside "cursor" and "order_by".
        body: dict[str, Any] = {
            "query": query,
            "limit": max(1, min(limit, SEARCH_LIMIT_MAX)),
        }
        filters: dict[str, Any] = {}
        if result_type is not None:
            filters["type"] = result_type
        if cluster is not None:
            # Normalized here, like every other slug-shaped argument in this
            # client, because "cluster" is the strictest slug on the API.
            # `GET /pages?slug=` resolves "Users/Igor/MLflow", "users/igor/mlflow/"
            # and "/users/igor/mlflow" all to the same page, but the search
            # filter matches the stored slug literally: any of those three
            # spellings answers 200 with zero results, indistinguishable from an
            # empty section (probed 2026-08-18). Leaving normalization to the
            # tool layer would mean a direct client caller silently gets nothing.
            filters["cluster"] = normalize_slug(cluster).lower()
        if authors:
            filters["authors"] = [
                author.model_dump(exclude_none=True) for author in authors
            ]
        if created_at is not None:
            filters["created_at"] = created_at.model_dump(by_alias=True)
        if modified_at is not None:
            filters["modified_at"] = modified_at.model_dump(by_alias=True)
        if filters:
            body["filters"] = filters
        if highlight:
            body["highlight"] = True
        payload = await self._request(
            "POST",
            "v1/search",
            json_body=body,
            auth=auth,
            retryable=True,
        )
        return SearchResponse.model_validate_json(payload)

    async def page_get_descendants(
        self,
        slug: str,
        *,
        include_self: bool = False,
        page_size: int = 100,
        cursor: str | None = None,
        auth: YandexAuth | None = None,
    ) -> DescendantsResponse:
        """Descendants of a page, or of the whole organization.

        An empty slug is not a mistake to guard against: the API reads
        ``?slug=`` as the root and answers with every page in the
        organization, all nesting levels, top-level pages included. It is a
        deliberate contract rather than a fallback for bad input — an
        unresolvable slug 404s instead (verified live 2026-08-10). A 404 on
        the empty slug therefore stays a plain API error: there is no page
        for ``PageNotFound("")`` to name.
        """
        normalized_slug = normalize_slug(slug)
        params: dict[str, Any] = {
            "slug": normalized_slug,
            "include_self": str(include_self).lower(),
            "page_size": page_size,
        }
        if cursor:
            params["cursor"] = cursor

        payload = await self._request(
            "GET",
            "v1/pages/descendants",
            params=params,
            auth=auth,
            not_found=(
                (lambda: PageNotFound(normalized_slug)) if normalized_slug else None
            ),
        )
        return DescendantsResponse.model_validate_json(payload)

    async def page_get_comments(
        self,
        page_id: int,
        *,
        page_size: int = 100,
        cursor: str | None = None,
        auth: YandexAuth | None = None,
    ) -> CommentsResponse:
        params: dict[str, Any] = {"page_size": page_size}
        if cursor:
            params["cursor"] = cursor

        payload = await self._request(
            "GET",
            f"v1/pages/{page_id}/comments",
            params=params,
            auth=auth,
            not_found=lambda: PageNotFound(page_id),
        )
        return CommentsResponse.model_validate_json(payload)

    async def page_get_resources(
        self,
        page_id: int,
        *,
        resource_types: list[str] | None = None,
        q: str | None = None,
        page_size: int = 50,
        cursor: str | None = None,
        order_by: str | None = None,
        order_direction: str | None = None,
        auth: YandexAuth | None = None,
    ) -> ResourcesResponse:
        params: dict[str, Any] = {"page_size": page_size}
        if resource_types:
            params["types"] = ",".join(resource_types)
        if q:
            params["q"] = q
        if cursor:
            params["cursor"] = cursor
        if order_by:
            params["order_by"] = order_by
        if order_direction:
            params["order_direction"] = order_direction

        payload = await self._request(
            "GET",
            f"v1/pages/{page_id}/resources",
            params=params,
            auth=auth,
            not_found=lambda: PageNotFound(page_id),
        )
        return ResourcesResponse.model_validate_json(payload)

    async def page_get_grids(
        self,
        page_id: int,
        *,
        page_size: int = 50,
        cursor: str | None = None,
        order_by: str | None = None,
        order_direction: str | None = None,
        auth: YandexAuth | None = None,
    ) -> GridsResponse:
        params: dict[str, Any] = {"page_size": page_size}
        if cursor:
            params["cursor"] = cursor
        if order_by:
            params["order_by"] = order_by
        if order_direction:
            params["order_direction"] = order_direction

        payload = await self._request(
            "GET",
            f"v1/pages/{page_id}/grids",
            params=params,
            auth=auth,
            not_found=lambda: PageNotFound(page_id),
        )
        return GridsResponse.model_validate_json(payload)

    async def grid_get(
        self,
        grid_id: str,
        *,
        fields: list[str] | None = None,
        filter: str | None = None,
        only_cols: str | None = None,
        only_rows: str | None = None,
        revision: str | None = None,
        sort: str | None = None,
        auth: YandexAuth | None = None,
    ) -> WikiGrid:
        params: dict[str, Any] = {}
        if fields:
            params["fields"] = ",".join(fields)
        if filter:
            params["filter"] = filter
        if only_cols:
            params["only_cols"] = only_cols
        if only_rows:
            params["only_rows"] = only_rows
        if revision:
            params["revision"] = revision
        if sort:
            params["sort"] = sort

        payload = await self._request(
            "GET",
            f"v1/grids/{grid_id}",
            params=params if params else None,
            auth=auth,
            not_found=lambda: GridNotFound(grid_id),
        )
        return WikiGrid.model_validate_json(payload)

    async def grid_create(
        self,
        *,
        request: GridCreateRequest,
        auth: YandexAuth | None = None,
    ) -> WikiGrid:
        payload = await self._request(
            "POST",
            "v1/grids",
            json_body=request.model_dump(exclude_none=True),
            auth=auth,
        )
        return WikiGrid.model_validate_json(payload)

    async def grid_update(
        self,
        grid_id: str,
        *,
        request: GridUpdateRequest,
        auth: YandexAuth | None = None,
    ) -> GridUpdateResponse:
        body = request.model_dump(exclude_none=True)
        if not request.default_sort:
            body.pop("default_sort", None)

        payload = await self._request(
            "POST",
            f"v1/grids/{grid_id}",
            json_body=body,
            auth=auth,
            not_found=lambda: GridNotFound(grid_id),
        )
        return GridUpdateResponse.model_validate_json(payload)

    async def grid_add_rows(
        self,
        grid_id: str,
        *,
        revision: str,
        rows: list[dict[str, Any]],
        position: int | None = None,
        after_row_id: str | None = None,
        auth: YandexAuth | None = None,
    ) -> GridMutationResponse:
        body: dict[str, Any] = {
            "revision": revision,
            "rows": rows,
        }
        if position is not None:
            body["position"] = position
        if after_row_id is not None:
            body["after_row_id"] = after_row_id

        payload = await self._request(
            "POST",
            f"v1/grids/{grid_id}/rows",
            json_body=body,
            auth=auth,
            not_found=lambda: GridNotFound(grid_id),
        )
        return GridMutationResponse.model_validate_json(payload)

    async def grid_delete(
        self,
        grid_id: str,
        *,
        auth: YandexAuth | None = None,
    ) -> GridDeleteResponse:
        payload = await self._request(
            "DELETE",
            f"v1/grids/{grid_id}",
            auth=auth,
            not_found=lambda: GridNotFound(grid_id),
        )
        # The endpoint answers 204 No Content, so the acknowledgment fields
        # are ours; a future JSON object body still reaches the model, where
        # the contract sweep watches for undeclared keys. A non-object body
        # would be dropped here — nothing merges a bare list into a model.
        data = self._json_or_empty(payload)
        body = data if isinstance(data, dict) else {}
        return GridDeleteResponse.model_validate(
            {**body, "grid_id": grid_id, "deleted": True}
        )

    async def grid_copy(
        self,
        grid_id: str,
        *,
        target: str,
        title: str | None = None,
        auth: YandexAuth | None = None,
    ) -> GridOperationResponse:
        body: dict[str, Any] = {"target": target}
        if title is not None:
            body["title"] = title

        payload = await self._request(
            "POST",
            f"v1/grids/{grid_id}/clone",
            json_body=body,
            auth=auth,
            not_found=lambda: GridNotFound(grid_id),
        )
        return GridOperationResponse.model_validate(self._json_or_empty(payload))

    async def grid_update_cells(
        self,
        grid_id: str,
        *,
        cells: list[dict[str, Any]],
        auth: YandexAuth | None = None,
    ) -> GridCellsResponse:
        payload = await self._request(
            "POST",
            f"v1/grids/{grid_id}/cells",
            json_body={"cells": cells},
            auth=auth,
            not_found=lambda: GridNotFound(grid_id),
        )
        return GridCellsResponse.model_validate(self._json_or_empty(payload))

    async def grid_delete_rows(
        self,
        grid_id: str,
        *,
        revision: str,
        row_ids: list[str],
        auth: YandexAuth | None = None,
    ) -> GridMutationResponse:
        payload = await self._request(
            "DELETE",
            f"v1/grids/{grid_id}/rows",
            json_body={"revision": revision, "row_ids": row_ids},
            auth=auth,
            not_found=lambda: GridNotFound(grid_id),
        )
        return GridMutationResponse.model_validate(self._json_or_empty(payload))

    async def grid_add_columns(
        self,
        grid_id: str,
        *,
        revision: str,
        columns: list[dict[str, Any]],
        position: int | None = None,
        auth: YandexAuth | None = None,
    ) -> GridMutationResponse:
        body: dict[str, Any] = {
            "revision": revision,
            "columns": columns,
        }
        if position is not None:
            body["position"] = position

        payload = await self._request(
            "POST",
            f"v1/grids/{grid_id}/columns",
            json_body=body,
            auth=auth,
            not_found=lambda: GridNotFound(grid_id),
        )
        return GridMutationResponse.model_validate(self._json_or_empty(payload))

    async def grid_delete_columns(
        self,
        grid_id: str,
        *,
        revision: str,
        column_slugs: list[str],
        auth: YandexAuth | None = None,
    ) -> GridMutationResponse:
        payload = await self._request(
            "DELETE",
            f"v1/grids/{grid_id}/columns",
            json_body={"revision": revision, "column_slugs": column_slugs},
            auth=auth,
            not_found=lambda: GridNotFound(grid_id),
        )
        return GridMutationResponse.model_validate(self._json_or_empty(payload))

    async def grid_move_row(
        self,
        grid_id: str,
        *,
        revision: str,
        row_id: str,
        position: int | None = None,
        after_row_id: str | None = None,
        auth: YandexAuth | None = None,
    ) -> GridMutationResponse:
        body: dict[str, Any] = {
            "revision": revision,
            "row_id": row_id,
        }
        if position is not None:
            body["position"] = position
        if after_row_id is not None:
            body["after_row_id"] = after_row_id

        payload = await self._request(
            "POST",
            f"v1/grids/{grid_id}/rows/move",
            json_body=body,
            auth=auth,
            not_found=lambda: GridNotFound(grid_id),
        )
        return GridMutationResponse.model_validate(self._json_or_empty(payload))

    async def grid_move_column(
        self,
        grid_id: str,
        *,
        revision: str,
        column_slug: str,
        position: int,
        auth: YandexAuth | None = None,
    ) -> GridMutationResponse:
        payload = await self._request(
            "POST",
            f"v1/grids/{grid_id}/columns/move",
            json_body={
                "revision": revision,
                "column_slug": column_slug,
                "position": position,
            },
            auth=auth,
            not_found=lambda: GridNotFound(grid_id),
        )
        return GridMutationResponse.model_validate(self._json_or_empty(payload))

    async def page_get_attachments(
        self,
        page_id: int,
        *,
        page_size: int = 100,
        cursor: str | None = None,
        auth: YandexAuth | None = None,
    ) -> AttachmentListResponse:
        params: dict[str, Any] = {"page_size": page_size}
        if cursor:
            params["cursor"] = cursor

        payload = await self._request(
            "GET",
            f"v1/pages/{page_id}/attachments",
            params=params,
            auth=auth,
            not_found=lambda: PageNotFound(page_id),
        )
        return AttachmentListResponse.model_validate_json(payload)

    async def page_create(
        self,
        *,
        slug: str,
        title: str,
        content: str,
        auth: YandexAuth | None = None,
    ) -> WikiPage:
        body = {
            "slug": normalize_slug(slug),
            "title": title,
            "content": content,
        }
        payload = await self._request("POST", "v1/pages", json_body=body, auth=auth)
        return WikiPage.model_validate_json(payload)

    async def page_clone(
        self,
        page_id: int,
        *,
        target: str,
        title: str | None = None,
        auth: YandexAuth | None = None,
    ) -> ClonedPageRef:
        # The API's only relocation primitive. There is no move/rename:
        # POST /pages/{id} silently ignores a "slug" field (the documented
        # body is title/content/redirect/access_policy/owner) and no /move
        # endpoint exists — probed live 2026-08-08, docs/api-notes.md. Clone
        # copies a single page (children stay behind) to a new slug with a
        # new id, as a deferred operation that is polled here to completion
        # so callers get the copy's id and slug, not a status URL.
        normalized = normalize_slug(target)
        if not normalized:
            raise ValueError("target must not be empty.")

        body: dict[str, Any] = {"target": normalized}
        if title is not None:
            body["title"] = title

        payload = await self._request(
            "POST",
            f"v1/pages/{page_id}/clone",
            json_body=body,
            auth=auth,
            not_found=lambda: PageNotFound(page_id),
        )
        started = GridOperationResponse.model_validate(self._json_or_empty(payload))
        if not started.status_url:
            raise WikiOperationError(
                "clone operation did not return a status_url to poll; "
                "whether the copy was created is unknown"
            )

        deadline = asyncio.get_running_loop().time() + CLONE_POLL_TIMEOUT
        while True:
            status_payload = await self._request(
                "GET",
                started.status_url.lstrip("/"),
                auth=auth,
            )
            progress = PageCloneStatus.model_validate_json(status_payload)
            if progress.status == "success":
                if progress.result is None or progress.result.page is None:
                    raise WikiOperationError(
                        "clone operation succeeded but reported no page"
                    )
                return progress.result.page
            if progress.status in CLONE_FAILED_STATUSES:
                raise WikiOperationError(
                    f"clone operation ended with status={progress.status!r}"
                )
            if asyncio.get_running_loop().time() >= deadline:
                raise WikiOperationError(
                    f"clone operation did not finish within {CLONE_POLL_TIMEOUT:.0f}s; "
                    f"check {started.status_url} manually"
                )
            await asyncio.sleep(CLONE_POLL_INTERVAL)

    async def page_update(
        self,
        page_id: int,
        *,
        title: str | None = None,
        content: str | None = None,
        redirect_to_page_id: int | None = None,
        clear_redirect: bool = False,
        allow_merge: bool = False,
        is_silent: bool = False,
        auth: YandexAuth | None = None,
    ) -> WikiPage:
        validate_page_update_args(
            title=title,
            content=content,
            redirect_to_page_id=redirect_to_page_id,
            clear_redirect=clear_redirect,
        )

        body: dict[str, Any] = {}
        if title is not None:
            body["title"] = title
        if content is not None:
            body["content"] = content
        # Wire shape verified live 2026-08-11 (docs/api-notes.md):
        # {"page": {"id": N}} sets a redirect, {"page": null} clears it.
        if redirect_to_page_id is not None:
            body["redirect"] = {"page": {"id": redirect_to_page_id}}
        elif clear_redirect:
            body["redirect"] = {"page": None}

        params: dict[str, Any] = {}
        if allow_merge:
            params["allow_merge"] = "true"
        if is_silent:
            params["is_silent"] = "true"

        payload = await self._request(
            "POST",
            f"v1/pages/{page_id}",
            params=params if params else None,
            json_body=body,
            auth=auth,
            not_found=lambda: PageNotFound(page_id),
        )
        return WikiPage.model_validate_json(payload)

    async def page_append_content(
        self,
        page_id: int,
        *,
        content: str,
        location: UploadLocation = "bottom",
        anchor: str | None = None,
        auth: YandexAuth | None = None,
    ) -> WikiPage:
        body: dict[str, Any] = {"content": content}
        if anchor:
            body["anchor"] = {"name": anchor}
        else:
            body["body"] = {"location": location}

        try:
            payload = await self._request(
                "POST",
                f"v1/pages/{page_id}/append-content",
                json_body=body,
                auth=auth,
                not_found=lambda: PageNotFound(page_id),
            )
        except WikiApiError as exc:
            if not (
                anchor and exc.status == 400 and exc.error_code == "ANCHOR_NOT_FOUND"
            ):
                raise
            page = await self.page_get(page_id, fields=["content"], auth=auth)
            if isinstance(page.content, str):
                updated_content = append_content_to_anchor_source(
                    page.content,
                    appended_content=content,
                    anchor=anchor,
                )
                if updated_content is not None:
                    return await self.page_update(
                        page_id,
                        content=updated_content,
                        allow_merge=True,
                        auth=auth,
                    )
            raise
        # The endpoint answers with the full updated page, not a status stub
        # (verified live, docs/api-notes.md) — same shape as the anchor
        # fallback above, so both paths of this tool agree.
        return WikiPage.model_validate_json(payload)

    async def page_add_comment(
        self,
        page_id: int,
        *,
        body: str,
        parent_id: int | None = None,
        thread_id: int | None = None,
        auth: YandexAuth | None = None,
    ) -> PageComment:
        request_body: dict[str, Any] = {"body": body}
        if parent_id is not None:
            request_body["parent_id"] = parent_id
        if thread_id is not None:
            request_body["thread_id"] = thread_id

        payload = await self._request(
            "POST",
            f"v1/pages/{page_id}/comments",
            json_body=request_body,
            auth=auth,
            not_found=lambda: PageNotFound(page_id),
        )
        return PageComment.model_validate_json(payload)

    async def page_delete_comment(
        self,
        page_id: int,
        *,
        comment_id: int,
        auth: YandexAuth | None = None,
    ) -> DeleteCommentResponse:
        # No not_found mapping: a 404 here is ambiguous (page or comment
        # missing), and the API's own envelope already names the culprit —
        # debug_message says "No Comment matches the given query." for a
        # bogus comment id (probed 2026-08-11). PageNotFound would mislabel
        # that case.
        payload = await self._request(
            "DELETE",
            f"v1/pages/{page_id}/comments/{comment_id}",
            auth=auth,
        )
        # The 200 body carries comments_count, but that is the only field it
        # carries, so an empty body (or a renamed key) would dump to `{}` once
        # _drop_none runs — a successful call with no evidence in it. The id
        # pair and `deleted` are built here as the floor, mirroring
        # page_delete_attachment; the isinstance guard mirrors grid_delete, so
        # a non-object body cannot raise a bare ValidationError past WikiError.
        data = self._json_or_empty(payload)
        body = data if isinstance(data, dict) else {}
        return DeleteCommentResponse.model_validate(
            {**body, "page_id": page_id, "comment_id": comment_id, "deleted": True}
        )

    async def page_read_attachment_bytes(
        self,
        page_id: int,
        *,
        file_id: int,
        max_bytes: Callable[[str | None], int] | None = None,
        auth: YandexAuth | None = None,
    ) -> AttachmentContent:
        # not_found IS mapped here, unlike the deletes: this endpoint
        # answers a miss with a placeholder GIF body instead of the JSON
        # error envelope (probed 2026-08-11), which build_api_error would
        # reduce to a bare "status 404".
        #
        # retryable=False although this is a GET: every other GET returns a
        # small JSON body where a repeat costs nothing, while here a repeat
        # re-transfers the whole file. The caller wants one attempt and a
        # clear error, not three transfers of the same blob.
        body, mime_type = await self._request_raw(
            "GET",
            f"v1/pages/{page_id}/attachments/{file_id}/download",
            auth=auth,
            not_found=lambda: AttachmentNotFound(page_id, file_id),
            retryable=False,
            max_bytes=max_bytes,
        )
        return AttachmentContent(body, mime_type)

    async def page_download_attachment(
        self,
        page_id: int,
        *,
        file_id: int,
        save_to: str,
        overwrite: bool = False,
        auth: YandexAuth | None = None,
    ) -> AttachmentDownloadResult:
        """Stream an attachment to a local file, never holding it in memory.

        The inline reader above exists to bring content into the conversation
        and is capped accordingly; this is the uncapped counterpart for getting
        the artifact itself. Chunks go from the socket to a `.part` file in the
        target directory, fsynced and put in place under the final name only on
        success, so an interrupted transfer never leaves a half-written file
        there and a pre-existing file survives any failure intact.

        The `.part` is created on the first chunk, not up front: a 404 or a
        403 then leaves the caller's filesystem untouched instead of creating
        a directory tree and a temp file for a body that never arrives.
        """
        target = local_path(save_to)

        # Every filesystem operation for this download runs on ONE dedicated
        # worker, in submission order. That is what makes the fd safe: it is
        # never touched from the event loop, and a cancelled `await` cannot
        # close a descriptor another thread is still writing to — the write and
        # the cleanup are queued on the same worker, so cleanup simply runs
        # after the write is done with it — replacing the lock plus the
        # `abandoned` flag that were hand-rolling that ordering before.
        #
        # Shut down with wait=False, and never via `with`: the context
        # manager's exit calls shutdown(wait=True) on the event loop thread,
        # so a cancellation with a write in flight froze the whole server
        # until that write returned — measured at 951 ms against a 1 s stub,
        # and unbounded on a hung mount. wait=False still drains the queue
        # (already-submitted work always runs), so the cleanup happens; it
        # just stops happening on the loop.
        loop = asyncio.get_running_loop()
        # `holder` is mutated ONLY by the worker; the event loop reads it, but
        # always after awaiting the hand-off that filled it, so it needs no
        # lock: the single worker gives the ordering a lock was previously
        # faking. A cancelled `await` cannot stop the open that is
        # already running, but the cleanup submitted afterwards is queued
        # behind it and therefore sees whatever it produced.
        holder: list[tuple[int, Path]] = []
        size = 0
        head = b""

        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wiki-download")
        try:

            def run(fn: Callable[[], _T]) -> Awaitable[_T]:
                return loop.run_in_executor(pool, fn)

            # Refused before the request, so a bad path costs no transfer. The
            # same checks run again inside _open_part, which is where they are
            # authoritative — this pair only buys the early exit.
            await run(lambda: _probe_target(target, overwrite=overwrite))

            def open_part() -> None:
                holder.append(_open_part(target, overwrite=overwrite))

            def commit() -> None:
                # Pops before committing: _commit_part owns the fd and the file
                # from here, cleans up its own failures, and the cleanup task
                # must therefore never see this pair again.
                fd, part = holder.pop()
                _commit_part(fd, part, target, overwrite=overwrite)

            def discard_leftover() -> None:
                if holder:
                    _discard_part(*holder.pop())

            async def sink(chunk: bytes) -> None:
                nonlocal size, head
                if len(head) < IMAGE_MAGIC_PREFIX_BYTES:
                    head = (head + chunk)[:IMAGE_MAGIC_PREFIX_BYTES]
                if not holder:
                    # Created on the first chunk, not up front, so a 404 or a
                    # 403 leaves the caller's filesystem untouched.
                    await run(open_part)
                fd = holder[0][0]
                size += len(chunk)
                try:
                    await run(lambda: _write_all(fd, chunk))
                except OSError as exc:
                    raise WikiLocalFileError(
                        f"Cannot write to {target}", cause=exc
                    ) from exc

            try:
                _, wire_mime = await self._request_raw(
                    "GET",
                    f"v1/pages/{page_id}/attachments/{file_id}/download",
                    auth=auth,
                    not_found=lambda: AttachmentNotFound(page_id, file_id),
                    retryable=False,
                    body_sink=sink,
                    timeout=DOWNLOAD_STALL_TIMEOUT,
                )
                if not holder:
                    # An empty attachment: no chunk ever arrived, so the file
                    # still has to be created before it can be committed.
                    await run(open_part)
                await run(commit)
            except BaseException:
                # BaseException on purpose: CancelledError is not an Exception,
                # and a cancelled transfer must not leak its `.part` either.
                # Submitted rather than awaited — the await would just be
                # cancelled again — and queued behind any open or write still
                # running, so it sees the finished state rather than racing it.
                pool.submit(discard_leftover)
                raise
            resolved = await run(target.resolve)
        finally:
            pool.shutdown(wait=False)
        return AttachmentDownloadResult(
            page_id=page_id,
            file_id=file_id,
            path=str(resolved),
            size_bytes=size,
            # The sniffed type wins over the header for the same reason the
            # inline read trusts bytes over claims — and so that one file does
            # not report image/png when read and application/octet-stream when
            # saved. The header remains the answer for everything the magic
            # table does not cover.
            mimetype=image_mime(head) or wire_mime,
        )

    async def page_delete_attachment(
        self,
        page_id: int,
        *,
        file_id: int,
        auth: YandexAuth | None = None,
    ) -> AttachmentDeleteResponse:
        # No not_found mapping: the 404 envelope names the culprit itself —
        # "No File matches the given query." (probed 2026-08-11).
        payload = await self._request(
            "DELETE",
            f"v1/pages/{page_id}/attachments/{file_id}",
            auth=auth,
        )
        # 204 No Content (documented and verified live): the acknowledgment
        # fields are ours. Merged rather than constructed, exactly like
        # grid_delete — any JSON object the API starts sending still reaches
        # the model, where the contract sweep watches for undeclared keys.
        # Constructing it directly would make that drift undetectable.
        data = self._json_or_empty(payload)
        body = data if isinstance(data, dict) else {}
        return AttachmentDeleteResponse.model_validate(
            {**body, "page_id": page_id, "file_id": file_id, "deleted": True}
        )

    async def user_get_current(
        self,
        *,
        auth: YandexAuth | None = None,
    ) -> WikiCurrentUser:
        payload = await self._request("GET", "v1/users/me", auth=auth)
        return WikiCurrentUser.model_validate_json(payload)

    async def page_delete(
        self,
        page_id: int,
        *,
        auth: YandexAuth | None = None,
    ) -> DeletePageResponse:
        payload = await self._request(
            "DELETE",
            f"v1/pages/{page_id}",
            auth=auth,
            not_found=lambda: PageNotFound(page_id),
        )
        return DeletePageResponse.model_validate_json(payload)

    async def page_recover(
        self,
        recovery_token: str,
        *,
        auth: YandexAuth | None = None,
    ) -> RecoverPageResponse:
        payload = await self._request(
            "POST",
            f"v1/recovery_tokens/{recovery_token}/recover",
            auth=auth,
        )
        return RecoverPageResponse.model_validate_json(payload)

    async def upload_session_create(
        self,
        *,
        file_name: str,
        file_size: int,
        auth: YandexAuth | None = None,
    ) -> UploadSessionResponse:
        payload = await self._request(
            "POST",
            "v1/upload_sessions",
            json_body={"file_name": file_name, "file_size": file_size},
            auth=auth,
        )
        return UploadSessionResponse.model_validate_json(payload)

    async def _upload_part(
        self,
        session_id: str,
        *,
        part_number: int,
        data: bytes,
        auth: YandexAuth | None = None,
    ) -> None:
        await self._request(
            "PUT",
            f"v1/upload_sessions/{session_id}/upload_part",
            params={"part_number": part_number},
            data=data,
            content_type="application/octet-stream",
            auth=auth,
            timeout=self._upload_timeout,
            # Re-uploading a part by its number within a session overwrites it.
            retryable=True,
        )

    async def _finish_upload_session(
        self,
        session_id: str,
        *,
        auth: YandexAuth | None = None,
    ) -> None:
        await self._request(
            "POST",
            f"v1/upload_sessions/{session_id}/finish",
            auth=auth,
            timeout=self._upload_timeout,
        )

    async def page_attach_upload_sessions(
        self,
        page_id: int,
        *,
        session_ids: list[str],
        auth: YandexAuth | None = None,
    ) -> AttachmentResultsResponse:
        payload = await self._request(
            "POST",
            f"v1/pages/{page_id}/attachments",
            json_body={"upload_sessions": session_ids},
            auth=auth,
            not_found=lambda: PageNotFound(page_id),
        )
        return AttachmentResultsResponse.model_validate_json(payload)

    async def page_upload_attachment(
        self,
        page_id: int,
        *,
        file_path: str,
        append_markup: bool = False,
        append_location: UploadLocation = "bottom",
        auth: YandexAuth | None = None,
    ) -> UploadAttachmentResult:
        # local_path, not Path: `~/report.pdf` has to mean the same thing here
        # as it does to page_download_attachment's save_to. WikiLocalFileError
        # rather than the builtin FileNotFoundError so both filesystem tools
        # fail inside the WikiError hierarchy every caller above already
        # handles.
        path = local_path(file_path)
        if not await asyncio.to_thread(path.is_file):
            raise WikiLocalFileError(f"File not found: {path}")

        stat_result = await asyncio.to_thread(path.stat)
        upload_session = await self.upload_session_create(
            file_name=path.name,
            file_size=stat_result.st_size,
            auth=auth,
        )

        handle = await asyncio.to_thread(_open_binary, path)
        try:
            part_number = 1
            while True:
                chunk = await asyncio.to_thread(handle.read, self.CHUNK_SIZE)
                if not chunk:
                    break
                await self._upload_part(
                    upload_session.session_id,
                    part_number=part_number,
                    data=chunk,
                    auth=auth,
                )
                part_number += 1
        finally:
            await asyncio.to_thread(handle.close)

        await self._finish_upload_session(upload_session.session_id, auth=auth)
        attachment_result = await self.page_attach_upload_sessions(
            page_id,
            session_ids=[upload_session.session_id],
            auth=auth,
        )

        appended_content: str | None = None
        if append_markup and attachment_result.results:
            first_attachment = attachment_result.results[0]
            appended_content = f'{{% file src="{first_attachment.download_url}" name="{first_attachment.name}" %}}'
            await self.page_append_content(
                page_id,
                content=appended_content,
                location=append_location,
                auth=auth,
            )

        return UploadAttachmentResult(
            page_id=page_id,
            attachments=attachment_result.results,
            appended_markup=append_markup,
            appended_content=appended_content,
        )
