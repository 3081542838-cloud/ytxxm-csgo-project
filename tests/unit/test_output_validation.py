from dataclasses import replace
import json
from pathlib import Path

import pytest

from cs2pov.adapters.video import FileSignature, VideoMetadata
from cs2pov.services.output_validation import (
    OutputDiscoveryRequest, OutputDiscoveryResult, OutputValidationRequest, OutputValidationResult,
    OutputValidationError, candidate_id_for, decode_discovery_request, decode_discovery_result,
    decode_validation_request, decode_validation_result, discover_output_files,
    output_json_value, validate_output,
)


def fixture(tmp_path):
    root = tmp_path / 'videos'
    file = FileSignature(root / 'Counter-strike 2' / 'clip.mp4', 12, 123_000_000_000,
                         122_000_000_000, ('windows', 77, 88), 124_000_000_000)
    request = OutputValidationRequest('a' * 32, 'b' * 32, str(root), file, 120., 130.)
    metadata = VideoMetadata(file.path, 4.5, 1920, 1080, 1, 1, file)
    return request, metadata


def test_all_worker_protocol_dtos_round_trip_as_strict_json_without_filesystem_access(tmp_path):
    validation, metadata = fixture(tmp_path)
    discovery = OutputDiscoveryRequest(validation.task_id, validation.request_id,
        validation.output_directory, validation.started_wall, validation.stopped_wall, (validation.candidate,))
    for value, decoder in (
        (discovery, decode_discovery_request),
        (OutputDiscoveryResult(discovery.task_id, discovery.request_id, (validation.candidate,)), decode_discovery_result),
        (validation, decode_validation_request),
        (OutputValidationResult(validation.task_id, validation.request_id, metadata), decode_validation_result),
    ):
        raw = json.loads(json.dumps(output_json_value(value), allow_nan=False))
        assert decoder(raw) == value
    assert not Path(validation.output_directory).exists()


@pytest.mark.parametrize('decoder_index', range(4))
@pytest.mark.parametrize('change', ['missing', 'extra', 'task_bool', 'nonce_empty', 'not_object'])
def test_worker_protocol_rejects_missing_unknown_and_wrongly_typed_envelope_fields(tmp_path, decoder_index, change):
    request, metadata = fixture(tmp_path)
    envelopes = (
        (OutputDiscoveryRequest(request.task_id, request.request_id, request.output_directory, 120., 130., ()), decode_discovery_request),
        (OutputDiscoveryResult(request.task_id, request.request_id, (request.candidate,)), decode_discovery_result),
        (request, decode_validation_request),
        (OutputValidationResult(request.task_id, request.request_id, metadata), decode_validation_result),
    )
    value, decoder = envelopes[decoder_index]
    raw = output_json_value(value)
    if change == 'missing': raw.pop('request_id')
    elif change == 'extra': raw['verified'] = True
    elif change == 'task_bool': raw['task_id'] = True
    elif change == 'nonce_empty': raw['request_id'] = ''
    else: raw = [raw]
    with pytest.raises(OutputValidationError): decoder(raw)


@pytest.mark.parametrize('value', [None, True, float('nan'), float('inf'), -1, 10**1000],
                         ids=['none', 'bool', 'nan', 'inf', 'negative', 'huge_int'])
def test_worker_protocol_rejects_invalid_wall_window_as_data_error(tmp_path, value):
    request, _ = fixture(tmp_path)
    raw = output_json_value(request); raw['started_wall'] = value
    with pytest.raises(OutputValidationError): decode_validation_request(raw)


@pytest.mark.parametrize('changed', [
    {'identity': None}, {'identity': ['unknown', 1, 2]}, {'identity': ['windows', 1, True]},
    {'identity': ['windows', 1, 0]}, {'change_ns': None}, {'bytes': True},
    {'mtime_ns': -1}, {'path': 'relative/clip.mp4'}, {'ctime_ns': 10**1000},
])
def test_worker_protocol_requires_complete_typed_file_identity(tmp_path, changed):
    request, _ = fixture(tmp_path)
    raw = output_json_value(request); raw['candidate'].update(changed)
    with pytest.raises(OutputValidationError): decode_validation_request(raw)


@pytest.mark.parametrize('path', ['outside', 'deep', 'traversal', 'wrong_suffix', 'ads'])
def test_worker_request_rejects_paths_outside_one_level_video_scope(tmp_path, path):
    request, _ = fixture(tmp_path)
    root = Path(request.output_directory)
    options = {'outside': tmp_path / 'elsewhere.mp4', 'deep': root / 'one' / 'two' / 'file.mp4',
        'traversal': root / '..' / 'clip.mp4', 'wrong_suffix': root / 'file.txt',
        'ads': root / 'clip.mp4:stream'}
    raw = output_json_value(request); raw['candidate']['path'] = str(options[path])
    with pytest.raises(OutputValidationError): decode_validation_request(raw)


def test_discovery_rejects_duplicate_and_over_limit_file_lists(tmp_path):
    request, _ = fixture(tmp_path)
    result = output_json_value(OutputDiscoveryResult(request.task_id, request.request_id, (request.candidate,)))
    result['candidates'] *= 2
    with pytest.raises(OutputValidationError): decode_discovery_result(result)
    result['candidates'] *= 2049
    with pytest.raises(OutputValidationError): decode_discovery_result(result)


@pytest.mark.parametrize('field,value', [('duration', 0), ('duration', True), ('duration', float('nan')),
    ('duration', 10**1000), ('width', 0), ('height', -1), ('video_streams', 0), ('audio_streams', 0),
    ('width', 1.5), ('audio_streams', True), ('signature', None)],
    ids=['duration_zero', 'duration_bool', 'duration_nan', 'duration_huge', 'width_zero',
         'height_negative', 'video_missing', 'audio_missing', 'width_float', 'audio_bool', 'no_signature'])
def test_metadata_protocol_rejects_unusable_streams_and_missing_identity(tmp_path, field, value):
    request, metadata = fixture(tmp_path)
    raw = output_json_value(OutputValidationResult(request.task_id, request.request_id, metadata))
    raw['metadata'][field] = value
    with pytest.raises(OutputValidationError): decode_validation_result(raw)


def test_metadata_protocol_rejects_path_mismatch_and_empty_signature(tmp_path):
    request, metadata = fixture(tmp_path)
    raw = output_json_value(OutputValidationResult(request.task_id, request.request_id, metadata))
    raw['metadata']['path'] = str(tmp_path / 'other.mp4')
    with pytest.raises(OutputValidationError): decode_validation_result(raw)
    raw = output_json_value(OutputValidationResult(request.task_id, request.request_id, metadata))
    raw['metadata']['signature']['bytes'] = 0
    with pytest.raises(OutputValidationError): decode_validation_result(raw)


def test_discovery_worker_uses_both_frozen_wall_times_and_fixed_stop_grace(tmp_path):
    request, _ = fixture(tmp_path)
    discovery = OutputDiscoveryRequest(request.task_id, request.request_id, request.output_directory,
                                       request.started_wall, request.stopped_wall, (request.candidate,))
    observed = []
    def finder(directory, before, **kwargs):
        observed.append((directory, before, kwargs)); return (request.candidate,)
    result = discover_output_files(discovery, finder=finder)
    assert result == OutputDiscoveryResult(request.task_id, request.request_id, (request.candidate,))
    directory, before, arguments = observed[0]
    assert directory == Path(request.output_directory) and before == {request.candidate.path: request.candidate}
    assert arguments == dict(started_wall=120., stopped_wall=132., cancel=None)
    assert discovery.stopped_wall == 130.


def test_pure_validator_stabilizes_identity_then_probes_exact_version_and_passes_trusted_executable(tmp_path):
    request, _ = fixture(tmp_path)
    grown = replace(request.candidate, bytes=20, change_ns=125_000_000_000)
    calls = []
    def stable(path, **kwargs):
        calls.append(('stable', path, kwargs)); return grown
    def probe(path, **kwargs):
        calls.append(('probe', path, kwargs)); return VideoMetadata(path, 4.5, 1920, 1080, 1, 1, grown)
    result = validate_output(request, stabilize=stable, probe=probe, ffprobe='C:/trusted/ffprobe.exe')
    assert calls == [('stable', request.candidate.path, dict(expected=request.candidate, cancel=None)),
        ('probe', grown.path, dict(expected=grown, cancel=None, ffprobe='C:/trusted/ffprobe.exe'))]
    assert result.metadata.signature == grown and request.candidate.bytes == 12


@pytest.mark.parametrize('port', ['stable_identity', 'probe_version'])
def test_pure_validator_rejects_replaced_identity_or_changed_probe_version(tmp_path, port):
    request, metadata = fixture(tmp_path)
    stable = replace(request.candidate, identity=('windows', 77, 99)) if port == 'stable_identity' else request.candidate
    probe_called = []
    def probe(*_args, **_kwargs):
        probe_called.append(True)
        return replace(metadata, signature=replace(request.candidate, bytes=13))
    with pytest.raises(OutputValidationError):
        validate_output(request, stabilize=lambda *_args, **_kwargs: stable, probe=probe)
    assert bool(probe_called) == (port == 'probe_version')


@pytest.mark.parametrize('boundary', ['before', 'after_stable', 'after_probe'])
def test_pure_validator_checks_cancellation_before_and_between_all_ports(tmp_path, boundary):
    request, metadata = fixture(tmp_path)
    cancelled = [boundary == 'before']
    calls = []
    def stable(*_args, **_kwargs):
        calls.append('stable'); cancelled[0] = boundary == 'after_stable'; return request.candidate
    def probe(*_args, **_kwargs):
        calls.append('probe'); cancelled[0] = boundary == 'after_probe'; return metadata
    with pytest.raises(OutputValidationError, match='取消'):
        validate_output(request, stabilize=stable, probe=probe, cancel=lambda: cancelled[0])
    assert calls == {'before': [], 'after_stable': ['stable'], 'after_probe': ['stable', 'probe']}[boundary]


def test_candidate_digest_covers_task_and_every_signature_field(tmp_path):
    request, _ = fixture(tmp_path)
    original = candidate_id_for(request.task_id, request.candidate)
    assert len(original) == 64 and candidate_id_for(request.task_id, request.candidate) == original
    assert candidate_id_for('c' * 32, request.candidate) != original
    for change in (dict(bytes=13), dict(mtime_ns=124_000_000_000), dict(ctime_ns=121_000_000_000),
                   dict(identity=('windows', 77, 99)), dict(change_ns=126_000_000_000),
                   dict(path=request.candidate.path.with_name('other.mp4'))):
        assert candidate_id_for(request.task_id, replace(request.candidate, **change)) != original
