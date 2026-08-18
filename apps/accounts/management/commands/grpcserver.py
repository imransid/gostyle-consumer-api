
from concurrent import futures

import grpc
from django.core.management.base import BaseCommand

from apps.grpc_gen import auth_pb2_grpc
from apps.grpc_gen.servicer import ConsumerAuthServicer


class Command(BaseCommand):
    help = "Start the gRPC server for internal service calls."

    def add_arguments(self, parser):
        parser.add_argument("--port", type=int, default=50051)

    def handle(self, *args, **options):
        port = options["port"]

        # Thread pool, because each call does a database query and blocks.
        # Ten is arbitrary but sane for now; tune it against real load, not
        # a guess.
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))

        auth_pb2_grpc.add_ConsumerAuthServicer_to_server(
            ConsumerAuthServicer(), server
        )

        # INSECURE is correct here: this listens only on the internal Docker
        # network and is never exposed through nginx. TLS would matter the
        # day this crosses a machine boundary.
        server.add_insecure_port(f"[::]:{port}")
        server.start()

        self.stdout.write(self.style.SUCCESS(f"gRPC server on :{port}"))
        server.wait_for_termination()