from __future__ import annotations
import angr


class setbuf(angr.SimProcedure):
    # pylint:disable=arguments-differ, unused-argument

    def run(self, stream, buf):
        return
