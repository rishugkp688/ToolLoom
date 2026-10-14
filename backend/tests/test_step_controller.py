from __future__ import annotations

from backend.runtime.step_controller import StepController


def test_duplicate_call_emits_note():
    controller = StepController()
    notes = []
    for _ in range(3):
        notes.extend(controller.note_call("t__x", {"a": 1}))
    assert any("identical arguments" in note.message for note in notes)


def test_distinct_arguments_do_not_trigger_duplicate_note():
    controller = StepController()
    notes = []
    for i in range(5):
        notes.extend(controller.note_call("t__x", {"i": i}))
    assert not any("identical arguments" in note.message for note in notes)


def test_error_cascade_refuses_after_three():
    controller = StepController()
    assert controller.note_result("t__x", True) is None
    assert controller.note_result("t__x", True) is None
    note = controller.note_result("t__x", True)
    assert note is not None
    assert controller.should_refuse("t__x") is True


def test_success_resets_error_streak():
    controller = StepController()
    controller.note_result("t__x", True)
    controller.note_result("t__x", True)
    controller.note_result("t__x", False)
    controller.note_result("t__x", True)
    assert controller.should_refuse("t__x") is False


def test_flood_note_only_without_text():
    quiet = StepController()
    notes = []
    for i in range(6):
        notes.extend(quiet.note_call("t__x", {"i": i}))
    assert any("without a user-facing answer" in note.message for note in notes)

    chatty = StepController()
    chatty.note_text()
    chatty_notes = []
    for i in range(6):
        chatty_notes.extend(chatty.note_call("t__x", {"i": i}))
    assert not any("without a user-facing answer" in note.message for note in chatty_notes)


def test_advice_is_capped():
    controller = StepController()
    for _ in range(3):
        controller.note_call("t__x", {"a": 1})
    advice = controller.advice()
    assert advice is not None
    assert advice.count("- ") <= 3
