# pylint:disable=too-many-boolean-expressions
from __future__ import annotations
from collections import defaultdict
import logging

import ailment
import cle

from angr.utils.funcid import is_function_security_check_cookie
from angr.analyses.decompiler.stack_item import StackItem, StackItemType
from .optimization_pass import OptimizationPass, OptimizationPassStage


_l = logging.getLogger(name=__name__)


class WinStackCanarySimplifier(OptimizationPass):
    """
    Removes stack canary checks from decompilation results for Windows PE files.

    we need to run this pass before performing any full-function simplification. Otherwise the effects of
    _security_cookie will be propagated.
    """

    ARCHES = [
        "X86",
        "AMD64",
    ]
    PLATFORMS = ["windows"]
    STAGE = OptimizationPassStage.AFTER_SINGLE_BLOCK_SIMPLIFICATION
    NAME = "Simplify stack canaries in Windows PE files"
    DESCRIPTION = __doc__.strip()

    def __init__(self, func, **kwargs):
        super().__init__(func, **kwargs)
        self._security_cookie_addr = None
        if isinstance(self.project.loader.main_object, cle.PE):
            self._security_cookie_addr = self.project.loader.main_object.load_config.get("SecurityCookie", None)

        self.analyze()

    def _check(self):
        if self._security_cookie_addr is None:
            return False, None

        # Check the first block and see if there is any statement reading data from _security_cookie
        init_stmts = self._find_canary_init_stmt()

        return init_stmts is not None, {"init_stmts": init_stmts}

    def _analyze(self, cache=None):
        init_stmts = None
        if cache is not None:
            init_stmts = cache.get("init_stmts", None)

        if init_stmts is None:
            init_stmts = self._find_canary_init_stmt()

        if init_stmts is None:
            return

        # Look for the statement that loads back canary value from the stack
        first_block, canary_init_stmt_ids = init_stmts
        canary_init_stmt = first_block.statements[canary_init_stmt_ids[-1]]
        # where is the stack canary stored?
        if not isinstance(canary_init_stmt, ailment.Stmt.Store) or not isinstance(
            canary_init_stmt.addr, ailment.Expr.StackBaseOffset
        ):
            _l.debug(
                "Unsupported canary storing location %s. Expects an ailment.Expr.StackBaseOffset.",
                canary_init_stmt.addr,
            )
            return

        store_offset = canary_init_stmt.addr.offset
        if not isinstance(store_offset, int):
            _l.debug("Unsupported canary storing offset %s. Expects an int.", store_offset)

        # The function should have at least one end point calling _security_check_cookie
        # note that (at least for now) we rely on FLIRT to identify the _security_check_cookie function inside the
        # binary.
        # TODO: Add function matching logic to this simplifier

        # Find all nodes with 0 out-degrees
        all_endpoint_addrs = [node.addr for node in self._func.graph.nodes() if self._func.graph.out_degree(node) == 0]

        # Before node duplication, each pair of canary-check-success and canary-check-failure nodes have a common
        # predecessor.
        # map endpoint addrs to their common predecessors
        pred_addr_to_endpoint_addrs: dict[int, set[int]] = defaultdict(set)
        for node_addr in all_endpoint_addrs:
            preds = self._func.graph.predecessors(self._func.get_node(node_addr))
            for pred in preds:
                pred_addr_to_endpoint_addrs[pred.addr].add(node_addr)

        found_endpoints = False
        for pred_addr in pred_addr_to_endpoint_addrs:
            # the predecessor should call _security_check_cookie
            endpoint_preds = list(self._get_blocks(pred_addr))
            if not endpoint_preds:
                continue
            if self._find_stmt_calling_security_check_cookie(endpoint_preds[0]) is None:
                _l.debug("The predecessor does not call _security_check_cookie().")
                continue

            nodes_to_process = []
            for pred in endpoint_preds:
                check_call_stmt_idx = self._find_stmt_calling_security_check_cookie(pred)
                if check_call_stmt_idx is None:
                    _l.debug("Cannot find the statement calling _security_check_cookie() in the predecessor.")
                    continue

                canary_storing_stmt_idx = (
                    self._find_amd64_canary_storing_stmt(pred, store_offset)
                    if self.project.arch.name == "AMD64"
                    else self._find_x86_canary_storing_stmt(pred, store_offset)
                )
                if canary_storing_stmt_idx is None:
                    _l.debug("Cannot find the canary check statement in the predecessor.")
                    continue

                return_addr_storing_stmt_idx = self._find_return_addr_storing_stmt(pred)
                if return_addr_storing_stmt_idx is None:
                    _l.debug("Cannot find the return address storing statement in the predecessor.")
                    continue

                nodes_to_process.append(
                    (pred, check_call_stmt_idx, canary_storing_stmt_idx, return_addr_storing_stmt_idx)
                )

            # Now patch this function.
            for pred, check_call_stmt_idx, canary_storing_stmt_idx, return_addr_storing_stmt_idx in nodes_to_process:
                # Patch the pred so that it jumps to the one that is not stack_chk_fail_caller
                stmts = []
                for stmt_idx, stmt in enumerate(pred.statements):
                    if stmt_idx in {check_call_stmt_idx, canary_storing_stmt_idx, return_addr_storing_stmt_idx}:
                        continue
                    stmts.append(stmt)
                pred_copy = pred.copy(statements=stmts)

                self._update_block(pred, pred_copy)

                found_endpoints = True

        if found_endpoints:
            # Remove the statement that loads the stack canary from fs
            first_block_copy = first_block.copy()
            for stmt_idx in sorted(canary_init_stmt_ids, reverse=True):
                first_block_copy.statements.pop(stmt_idx)
            self._update_block(first_block, first_block_copy)

            # update stack_items
            self.stack_items[store_offset] = StackItem(
                store_offset, canary_init_stmt.size, "canary", StackItemType.STACK_CANARY
            )

    def _find_canary_init_stmt(self):
        first_block = self._get_block(self._func.addr)
        if first_block is None:
            return None

        load_stmt_idx = None
        load_reg = None
        xor_stmt_idx = None
        xored_reg = None

        for idx, stmt in enumerate(first_block.statements):
            # if we are lucky and things get folded into one statement:
            if (
                isinstance(stmt, ailment.Stmt.Store)
                and isinstance(stmt.addr, ailment.Expr.StackBaseOffset)
                and isinstance(stmt.data, ailment.Expr.BinaryOp)
                and stmt.data.op == "Xor"
                and isinstance(stmt.data.operands[1], ailment.Expr.StackBaseOffset)
                and isinstance(stmt.data.operands[0], ailment.Expr.Load)
                and isinstance(stmt.data.operands[0].addr, ailment.Expr.Const)
            ):
                # Check addr: must be __security_cookie
                load_addr = stmt.data.operands[0].addr.value
                if load_addr == self._security_cookie_addr:
                    return first_block, [idx]
            # or if we are unlucky and the load and the xor are two different statements
            if (
                isinstance(stmt, ailment.Stmt.Assignment)
                and isinstance(stmt.dst, ailment.Expr.VirtualVariable)
                and stmt.dst.was_reg
                and isinstance(stmt.src, ailment.Expr.Load)
                and isinstance(stmt.src.addr, ailment.Expr.Const)
            ):
                load_addr = stmt.src.addr.value
                if load_addr == self._security_cookie_addr:
                    load_stmt_idx = idx
                    load_reg = stmt.dst.reg_offset
            if load_stmt_idx is not None and idx == load_stmt_idx + 1:
                if (
                    isinstance(stmt, ailment.Stmt.Assignment)
                    and isinstance(stmt.dst, ailment.Expr.VirtualVariable)
                    and stmt.dst.was_reg
                    and isinstance(stmt.src, ailment.Expr.BinaryOp)
                    and stmt.src.op == "Xor"
                    and isinstance(stmt.src.operands[0], ailment.Expr.VirtualVariable)
                    and stmt.src.operands[0].was_reg
                    and stmt.src.operands[0].reg_offset == load_reg
                    and isinstance(stmt.src.operands[1], ailment.Expr.StackBaseOffset)
                ):
                    xor_stmt_idx = idx
                    xored_reg = stmt.dst.reg_offset
                else:
                    break
            if xor_stmt_idx is not None and idx == xor_stmt_idx + 1:
                if (
                    isinstance(stmt, ailment.Stmt.Store)
                    and isinstance(stmt.addr, ailment.Expr.StackBaseOffset)
                    and isinstance(stmt.data, ailment.Expr.VirtualVariable)
                    and stmt.data.was_reg
                    and stmt.data.reg_offset == xored_reg
                ):
                    return first_block, [load_stmt_idx, xor_stmt_idx, idx]
                break

        return None

    def _find_amd64_canary_storing_stmt(self, block, canary_value_stack_offset):
        load_stmt_idx = None

        for idx, stmt in enumerate(block.statements):
            # when we are lucky, we have one instruction
            if (
                (
                    isinstance(stmt, ailment.Stmt.Assignment)
                    and isinstance(stmt.dst, ailment.Expr.VirtualVariable)
                    and stmt.dst.was_reg
                    and stmt.dst.reg_offset == self.project.arch.registers["rcx"][0]
                )
                and isinstance(stmt.src, ailment.Expr.BinaryOp)
                and stmt.src.op == "Xor"
            ):
                op0, op1 = stmt.src.operands
                if (
                    isinstance(op0, ailment.Expr.Load)
                    and isinstance(op0.addr, ailment.Expr.StackBaseOffset)
                    and op0.addr.offset == canary_value_stack_offset
                ) and isinstance(op1, ailment.Expr.StackBaseOffset):
                    # found it
                    return idx
            # or when we are unlucky, we have two instructions...
            if (
                isinstance(stmt, ailment.Stmt.Assignment)
                and isinstance(stmt.dst, ailment.Expr.VirtualVariable)
                and stmt.dst.reg_offset == self.project.arch.registers["rcx"][0]
                and isinstance(stmt.src, ailment.Expr.Load)
                and isinstance(stmt.src.addr, ailment.Expr.StackBaseOffset)
                and stmt.src.addr.offset == canary_value_stack_offset
            ):
                load_stmt_idx = idx
            if (
                load_stmt_idx is not None
                and idx >= load_stmt_idx + 1
                and (
                    isinstance(stmt, ailment.Stmt.Assignment)
                    and isinstance(stmt.dst, ailment.Expr.VirtualVariable)
                    and stmt.dst.was_reg
                    and isinstance(stmt.src, ailment.Expr.BinaryOp)
                    and stmt.src.op == "Xor"
                )
                and (
                    isinstance(stmt.src.operands[0], ailment.Expr.VirtualVariable)
                    and stmt.src.operands[0].was_reg
                    and stmt.src.operands[0].reg_offset == self.project.arch.registers["rcx"][0]
                    and isinstance(stmt.src.operands[1], ailment.Expr.StackBaseOffset)
                )
            ):
                return idx
        return None

    def _find_x86_canary_storing_stmt(self, block, canary_value_stack_offset):
        load_stmt_idx = None

        for idx, stmt in enumerate(block.statements):
            # when we are lucky, we have one instruction
            if (
                (
                    isinstance(stmt, ailment.Stmt.Assignment)
                    and isinstance(stmt.dst, ailment.Expr.VirtualVariable)
                    and stmt.dst.was_reg
                    and stmt.dst.reg_offset == self.project.arch.registers["eax"][0]
                )
                and isinstance(stmt.src, ailment.Expr.BinaryOp)
                and stmt.src.op == "Xor"
            ):
                op0, op1 = stmt.src.operands
                if (
                    isinstance(op0, ailment.Expr.Load)
                    and isinstance(op0.addr, ailment.Expr.StackBaseOffset)
                    and op0.addr.offset == canary_value_stack_offset
                ) and isinstance(op1, ailment.Expr.StackBaseOffset):
                    # found it
                    return idx
            # or when we are unlucky, we have two instructions...
            if (
                isinstance(stmt, ailment.Stmt.Assignment)
                and isinstance(stmt.dst, ailment.Expr.VirtualVariable)
                and stmt.dst.reg_offset == self.project.arch.registers["eax"][0]
                and isinstance(stmt.src, ailment.Expr.Load)
                and isinstance(stmt.src.addr, ailment.Expr.StackBaseOffset)
                and stmt.src.addr.offset == canary_value_stack_offset
            ):
                load_stmt_idx = idx
            if (
                load_stmt_idx is not None
                and idx >= load_stmt_idx + 1
                and (
                    isinstance(stmt, ailment.Stmt.Assignment)
                    and isinstance(stmt.dst, ailment.Expr.VirtualVariable)
                    and stmt.dst.was_reg
                    and isinstance(stmt.src, ailment.Expr.BinaryOp)
                    and stmt.src.op == "Xor"
                )
                and (
                    isinstance(stmt.src.operands[0], ailment.Expr.VirtualVariable)
                    and stmt.src.operands[0].was_reg
                    and stmt.src.operands[0].reg_offset == self.project.arch.registers["eax"][0]
                    and isinstance(stmt.src.operands[1], ailment.Expr.StackBaseOffset)
                )
            ):
                return idx
        return None

    @staticmethod
    def _find_return_addr_storing_stmt(block):
        for idx, stmt in enumerate(block.statements):
            if (
                isinstance(stmt, ailment.Stmt.Store)
                and isinstance(stmt.addr, ailment.Expr.StackBaseOffset)
                and isinstance(stmt.data, ailment.Expr.Const)
                and stmt.data.value == block.addr + block.original_size
            ):
                return idx
        return None

    def _find_stmt_calling_security_check_cookie(self, node):
        for idx, stmt in enumerate(node.statements):
            if isinstance(stmt, ailment.Stmt.Call) and isinstance(stmt.target, ailment.Expr.Const):
                const_target = stmt.target.value
                if const_target in self.kb.functions:
                    func = self.kb.functions.function(addr=const_target)
                    if func.name == "_security_check_cookie" or is_function_security_check_cookie(
                        func, self.project, self._security_cookie_addr
                    ):
                        return idx

        return None
