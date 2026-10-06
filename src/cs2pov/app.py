"""Local desktop entry point. No deployment or recording at startup."""
import argparse
from pathlib import Path
import sys
from PySide6.QtCore import QLockFile
from PySide6.QtWidgets import QApplication, QMessageBox
from cs2pov.services.workspace import Workspace
from cs2pov.ui.window import MainWindow


def main():
    if sys.argv[1:2] == ['--environment-worker']:
        from cs2pov.services.environment_worker import main as worker_main
        return worker_main(sys.argv[2:])
    if sys.argv[1:2] == ['--nvidia-status-worker']:
        from cs2pov.adapters.nvidia_status import worker_cli
        return worker_cli(sys.argv[2:])
    if sys.argv[1:2] == ['--package-smoke']:
        from cs2pov.services.package_smoke import main as smoke_main
        return smoke_main(sys.argv[2:])
    if sys.argv[1:2] == ['--demo-worker']:
        from cs2pov.services.demo_worker import main as worker_main
        # Existing parser CLI reads sys.argv directly.
        sys.argv = [sys.argv[0], *sys.argv[2:]]
        return worker_main()
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, help="Local data directory; separate from videos")
    parser.add_argument("--migrate-from", type=Path,
                        help="Explicitly copy settings/presets into a new portable profile")
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("CS2POVHelper")
    model = None
    try:
        from cs2pov.services.portable import (PortableError, data_directory, initialize_profile,
                                            migrate_profile, portable_root)
        root = portable_root() if args.data_dir is None else None
        directory = data_directory(args.data_dir)
        from cs2pov.storage.transaction import no_redirection
        no_redirection(directory)
        if args.migrate_from is not None:
            if root is None or args.data_dir is not None:
                raise PortableError("设置迁移只用于未指定 --data-dir 的完整便携包。")
            migrate_profile(args.migrate_from, directory)
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except PermissionError as error:
            if root is not None:
                raise PortableError("便携 data 文件夹无法写入，请将整个虾米pov文件夹解压到可写位置后运行。") from error
            raise
        lock = QLockFile(str(directory / "app.lock"))
        lock.setStaleLockTime(0)
        if not lock.tryLock(0):
            if root is not None and lock.error() == QLockFile.LockError.PermissionError:
                raise PortableError("便携 data 文件夹无法写入，请将整个虾米pov文件夹解压到可写位置后运行。")
            raise RuntimeError("这个数据目录已有应用运行；请回到原窗口。")
        if root is not None:
            initialize_profile(directory)
        from cs2pov.services.preview_session import PreviewSession
        from cs2pov.adapters.nvidia import check_input_available
        model = Workspace(directory, automatic_recording=True,
            input_check=check_input_available,
            preview_factory=lambda *args, **kwargs: PreviewSession(*args, automatic=True,
                                                                  second_precision=True, **kwargs))
        window = MainWindow(model)
        window.show()
        return app.exec()
    except Exception as error:
        QMessageBox.critical(None, "启动未完成", str(error) + "\n原始数据和恢复备份保留。")
        return 1
    finally:
        if model is not None:
            model.library.close()


if __name__ == "__main__":
    raise SystemExit(main())
