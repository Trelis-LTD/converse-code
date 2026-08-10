# Vendored `@trelis/converse` browser SDK

This is the preferred-form JavaScript source for `@trelis/converse` 0.10.0 from
the pinned upstream revision recorded in [UPSTREAM.json](UPSTREAM.json). The
voice page is a static application served by the Python CLI, so the SDK is
included in the wheel rather than fetched from a CDN at runtime.

Do not edit copied SDK files here. Regenerate them from a licensed source tree:

```bash
uv run scripts/vendor_converse_sdk.py /path/to/sdk/browser --commit <full-git-sha>
```

The update script refuses non-Apache source and copies `LICENSE`, `NOTICE`, and
the complete `THIRD_PARTY_LICENSES` directory. Use `--check` with the same source
and commit to verify an update. Do not vendor an npm artifact whose package
metadata or notice set does not match the licensed source release.

The SDK owns the direct Converse connection, browser microphone capture, echo
cancellation, streaming playback, reconnection/resume, tool controls, and
interruption handling. Converse Code owns scoped-key issuance, the acknowledged
localhost tool bridge, Claude Code tools, and UI integration.
