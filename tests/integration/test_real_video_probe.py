"""Existing short sample is a probe fixture, never new recording proof."""
from pathlib import Path
import pytest
from cs2pov.adapters.probe_tool import trusted_probe
from cs2pov.adapters.video import current_signature, probe_video, VideoError

# Fixed copy from formal session preview-6caa7e51d86b4c83828546adb4eaedb7.
# Independent ffprobe CLI metadata is stored beside the fixture. Keep this
# outside the user's output directory so clearing recordings cannot remove it.
SAMPLE = (Path(__file__).resolve().parents[2] / 'local-validation/probe-fixtures/'
          'nvidia-20261006-hotfix.mp4')


def test_authenticated_real_ffprobe_reads_existing_short_video_without_mutation():
    before = current_signature(SAMPLE)
    with trusted_probe() as executable:
        metadata = probe_video(SAMPLE, ffprobe=executable, expected=before)
    assert metadata.signature == before == current_signature(SAMPLE)
    assert metadata.width == 1400 and metadata.height == 1050
    assert metadata.video_streams == 1 and metadata.audio_streams >= 1
    assert metadata.duration == pytest.approx(64.425878, abs=.01)


def test_authenticated_real_ffprobe_rejects_corrupt_nonempty_mp4(tmp_path):
    path = tmp_path / 'corrupt.mp4'; path.write_bytes(b'not a valid movie')
    before = current_signature(path)
    with trusted_probe() as executable:
        with pytest.raises(VideoError, match='拒绝'):
            probe_video(path, ffprobe=executable, expected=before)
    assert path.read_bytes() == b'not a valid movie' and current_signature(path) == before
