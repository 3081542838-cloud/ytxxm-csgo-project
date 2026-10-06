import pytest

from cs2pov.adapters.game_compatibility import supports_patch, unsupported_patch_message


@pytest.mark.parametrize('patch', ['1.41.8.8','1.41.8.9'])
def test_only_the_two_qualified_game_patches_can_enter_automatic_replay(patch):
    assert supports_patch(patch) is True


@pytest.mark.parametrize('patch', [None,'','1.41.8.7','1.41.8.10','future',True,False,0,{},[],b'1.41.8.9'])
def test_unknown_missing_or_nontext_patch_cannot_enter_automatic_replay(patch):
    assert supports_patch(patch) is False


def test_unknown_patch_message_reports_detected_version_and_all_qualified_versions():
    message=unsupported_patch_message('1.41.8.10')
    assert all(version in message for version in ('1.41.8.10','1.41.8.8','1.41.8.9'))


@pytest.mark.parametrize('patch', [None,'',True,{},[]])
def test_missing_or_malformed_version_has_readable_missing_version_diagnostic(patch):
    message=unsupported_patch_message(patch)
    assert '无法读取' in message and '1.41.8.8' in message and '1.41.8.9' in message
