"""Recognizing an attachment's real type from its first bytes.

Lives in the Wiki layer, not beside the tool that reads attachments into the
conversation, because both sides of the attachment flow need it: the inline
read has to decide whether the bytes may travel as an image block, and the
download-to-disk path has to report a `mimetype` that agrees with it. The
alternative — importing from `mcp_wiki.mcp` into `mcp_wiki.wiki` — is the one
direction the layering forbids, and copying the table would let the two
answers drift for the same file. `normalize_slug` sits here for the same
reason.
"""

# The only formats the vision APIs decode, and therefore the only ones an
# attachment may travel as an image block under. This is not a style
# preference: an ImageContent block whose mime is outside this set makes the
# host's next model call fail with `invalid_request_error: Could not process
# image`, and hosts retry the same tool call — anthropics/claude-code#28279
# documents an SVG doing exactly that and taking the session with it.
RENDERABLE_IMAGE_MIMES = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp"}
)

# Content-Type values that say nothing about the payload. They get the image
# budget on a read, so the magic-byte check below can run on bytes that were
# actually fetched rather than being pre-empted by a header that knew nothing.
UNINFORMATIVE_MIMES = frozenset({"", "application/octet-stream", "binary/octet-stream"})

# Magic numbers for the same four formats. Checked against the bytes, not a
# filename — the download endpoint sends no Content-Disposition, so there is
# no filename to check.
_IMAGE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)

# Enough bytes for every signature above, including WebP's `RIFF....WEBP`.
IMAGE_MAGIC_PREFIX_BYTES = 12


def image_mime(raw: bytes) -> str | None:
    """The renderable image mime these bytes are, or None if they are not one.

    The bytes decide, never a Content-Type header: all four formats carry a
    reliable magic number, so the header adds nothing a correct file needs —
    while trusting it means a mislabelled or empty attachment becomes an image
    block nothing can decode. `raw` may be just the first chunk; only the
    leading `IMAGE_MAGIC_PREFIX_BYTES` are ever examined.
    """
    for magic, mime in _IMAGE_MAGIC:
        if raw.startswith(magic):
            return mime
    if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return "image/webp"
    return None
