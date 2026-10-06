"""Explicit patches qualified by a real local replay and restoration trial."""

SUPPORTED_PATCHES = frozenset({'1.41.8.8', '1.41.8.9'})


def supports_patch(patch):
    return type(patch) is str and patch in SUPPORTED_PATCHES


def unsupported_patch_message(patch):
    detected = patch if type(patch) is str and patch else '无法读取'
    supported = '、'.join(sorted(SUPPORTED_PATCHES))
    return (f'当前 CS2 版本为 {detected}，本应用已验证的版本为 {supported}。'
            '当前版本尚未通过兼容性验证，请使用适配此版本的应用更新。')
