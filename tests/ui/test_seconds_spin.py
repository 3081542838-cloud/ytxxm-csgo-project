from cs2pov.ui.seconds_spin import SecondsSpinBox


def test_whole_seconds_editing_preserves_imported_tick_value(qtbot):
    spin = SecondsSpinBox()
    qtbot.addWidget(spin)
    spin.setRange(0, 100_000)
    spin.setValue(196.90625)
    assert spin.text() == '197'
    assert spin.value() == 196.90625
    assert spin.singleStep() == 1
    assert spin.valueFromText('198.2') == 198
    spin.stepUp()
    assert spin.text() == '198'
