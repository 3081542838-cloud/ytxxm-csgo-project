"""User-selected direct hotkey flow; no NVIDIA UI observation or state claim."""


class HotkeyRecording:
    def __init__(self, task, *, activate_game=None):
        self.task = task
        self.error = ''
        self.cancelled = False
        self.activate_game = activate_game
        task.dispatch_mode = 'direct_hotkey'

    def poll(self):
        task = self.task
        if self.cancelled or task.terminal or task.cleanup_requested:
            return
        try:
            if task.state == 'awaiting_start_foreground' and self.activate_game is not None:
                self.activate_game()
            # These are command-driven transitions, explicitly marked in the
            # durable task snapshot; they are not observations of NVIDIA.
            if task.state == 'awaiting_not_recording':
                task.confirm_not_recording(task_id=task.task_id, step_token=task.confirmation_token)
            elif task.state == 'awaiting_started':
                task.confirm_started(task_id=task.task_id, step_token=task.confirmation_token)
            elif task.state == 'awaiting_stopped':
                # Input completed. Leave hardware state unverified and close
                # through the existing transaction, without a console dialog.
                task.workflow.confirm_stopped(step_token=task.confirmation_token)
                task._cleanup()
        except Exception as error:
            self.error = str(error)
            task.cancel()

    def cancel(self):
        self.cancelled = True
