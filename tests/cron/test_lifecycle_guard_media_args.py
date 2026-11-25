"""Regression tests: non-executable binary media paths >1 MiB are exempt from lifecycle script-body scanning."""

from __future__ import annotations

import pytest

from cron.lifecycle_guard import _iter_referenced_shell_scripts, scan_gateway_lifecycle


@pytest.mark.parametrize(
    ("ext", "header"),
    [
        (".wav", b"RIFF\x00\x00\x00\x00WAVE"),
        (".mp4", b"\x00\x00\x00\x18ftypmp42"),
        (".mov", b"\x00\x00\x00\x14ftypqt  "),
        (".m4a", b"\x00\x00\x00\x20ftypM4A "),
        (".mp3", b"ID3\x03\x00\x00\x00\x00\x00\x00"),
        (".png", b"\x89PNG\r\n\x1a\n"),
        (".jpg", b"\xff\xd8\xff\xe0"),
        (".jpeg", b"\xff\xd8\xff\xe1"),
        (".webp", b"RIFF\x00\x00\x00\x00WEBP"),
        (".npy", b"\x93NUMPY\x01\x00"),
        (".onnx", b"\x08\x07\x12\x04onnx"),
    ],
)
def test_oversized_non_executable_media_path_is_exempt(tmp_path, ext, header):
    media = tmp_path / f"sample{ext}"
    media.write_bytes(header + b"\x00" * (1024 * 1024 + 64))
    media.chmod(0o644)

    # Path token appears in command position (e.g. parenthesized arg or line-wrapped script token).
    cmd = f"ffmpeg -i ({media})"
    assert list(_iter_referenced_shell_scripts(cmd)) == [media]

    unsafe, refusal = scan_gateway_lifecycle(cmd)
    assert unsafe is False, f"unexpected refusal for {ext}: {refusal}"
    assert refusal is None


def test_shebang_or_executable_or_explicit_shell_media_extension_still_blocks(tmp_path):
    # 1. File with a #! shebang disguised with .wav extension must still fail closed (>1 MiB).
    shebang_wav = tmp_path / "disguised.wav"
    shebang_wav.write_bytes(b"#!/bin/sh\n" + b"# padding\n" * (120 * 1024))
    shebang_wav.chmod(0o644)
    cmd_shebang = f"echo ok; {shebang_wav}"
    assert list(_iter_referenced_shell_scripts(cmd_shebang)) == [shebang_wav]
    unsafe, refusal = scan_gateway_lifecycle(cmd_shebang)
    assert unsafe is True and refusal is not None

    # 2. Executable (+x) file with .png extension must still fail closed.
    exec_png = tmp_path / "exec.png"
    exec_png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * (1024 * 1024 + 64))
    exec_png.chmod(0o755)
    cmd_exec = f"echo ok; {exec_png}"
    assert list(_iter_referenced_shell_scripts(cmd_exec)) == [exec_png]
    unsafe2, refusal2 = scan_gateway_lifecycle(cmd_exec)
    assert unsafe2 is True and refusal2 is not None

    # 3. Explicit shell invocation (bash / source) on a .wav file must still scan/block.
    shell_wav = tmp_path / "evil.wav"
    shell_wav.write_bytes(b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * (1024 * 1024 + 64))
    shell_wav.chmod(0o644)
    for cmd_shell in (f"bash {shell_wav}", f"source {shell_wav}"):
        unsafe3, refusal3 = scan_gateway_lifecycle(cmd_shell)
        assert unsafe3 is True and refusal3 is not None

    # 4. Non-media non-executable oversized file (e.g. .sh) must still fail closed.
    big_sh = tmp_path / "big.sh"
    big_sh.write_bytes(b"# plain shell without shebang\n" * (40 * 1024))
    big_sh.chmod(0o644)
    unsafe4, refusal4 = scan_gateway_lifecycle(f"echo ok; {big_sh}")
    assert unsafe4 is True and refusal4 is not None
