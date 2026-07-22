from django.core.management.base import BaseCommand, CommandError

from threading import Thread

from config.game_circle_ipc import GameCircleIpcServer, ipc_capability_from_environment
from config.game_circle_service import GameCircleSessionService
from jobs.game_circle_refresh import GAME_CIRCLE_LOGIN_PROBE_TASK, GAME_CIRCLE_SESSION_REFRESH_TASK, enqueue_login_probe
from jobs.worker import consume_jobs


class Command(BaseCommand):
    help = "仅认领并执行游戏圈会话续接任务。"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--loop", action="store_true")
        parser.add_argument("--poll-seconds", type=float, default=2)

    def handle(self, *args, **options):
        if options["once"] == options["loop"]:
            raise CommandError("必须且只能传入 --once 或 --loop")
        service = GameCircleSessionService()
        ipc_server = GameCircleIpcServer(
            service.handle_ipc,
            capability=ipc_capability_from_environment(),
        )
        server_thread = Thread(target=ipc_server.serve_forever, name="game-circle-ipc", daemon=True)
        server_thread.start()
        try:
            enqueue_login_probe(source="worker-start")
            consume_jobs(
                self,
                {
                    GAME_CIRCLE_SESSION_REFRESH_TASK: service.refresh,
                    GAME_CIRCLE_LOGIN_PROBE_TASK: service.probe_login,
                },
                loop=options["loop"],
                poll_seconds=options["poll_seconds"],
            )
        finally:
            service.stop_accepting()
            ipc_server.shutdown()
            ipc_server.server_close()
