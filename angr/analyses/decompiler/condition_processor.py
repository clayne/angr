from __future__ import annotations
from collections import defaultdict, OrderedDict
from typing import Any
from collections.abc import Callable
from collections.abc import Generator
import operator
import logging

import angr.ailment as ailment
import claripy
import networkx
from unique_log_filter import UniqueLogFilter


from angr.utils.graph import GraphUtils
from angr.utils.graph import dominates, inverted_idoms
from angr.utils.ail import is_head_controlled_loop_block
from angr.block import Block, BlockNode
from angr.errors import AngrRuntimeError
from .peephole_optimizations import InvertNegatedLogicalConjunctionsAndDisjunctions, RemoveRedundantNots
from .structuring.structurer_nodes import (
    MultiNode,
    EmptyBlockNotice,
    SequenceNode,
    CodeNode,
    SwitchCaseNode,
    BreakNode,
    ConditionalBreakNode,
    LoopNode,
    ConditionNode,
    ContinueNode,
    CascadingConditionNode,
    IncompleteSwitchCaseNode,
)
from .graph_region import GraphRegion
from .utils import peephole_optimize_expr


l = logging.getLogger(__name__)
l.addFilter(UniqueLogFilter())


_UNIFIABLE_COMPARISONS = {
    "__ne__",
    "__gt__",
    "__ge__",
    "UGT",
    "UGE",
    "SGT",
    "SGE",
}


_INVERSE_OPERATIONS = {
    "__eq__": "__ne__",
    "__ne__": "__eq__",
    "__gt__": "__le__",
    "__lt__": "__ge__",
    "__ge__": "__lt__",
    "__le__": "__gt__",
    "ULT": "UGE",
    "UGE": "ULT",
    "UGT": "ULE",
    "ULE": "UGT",
    "SLT": "SGE",
    "SGE": "SLT",
    "SLE": "SGT",
    "SGT": "SLE",
}


#
# Util methods and mapping used during AIL AST to claripy AST conversion
#


def _op_with_unified_size(op, conv: Callable, operand0, operand1, ins_addr: int):
    # ensure operand1 is of the same size as operand0
    if isinstance(operand1, ailment.Expr.Const):
        # amazing - we do the easy thing here
        return op(conv(operand0, nobool=True, ins_addr=ins_addr), operand1.value)
    if operand1.bits == operand0.bits:
        return op(conv(operand0, nobool=True, ins_addr=ins_addr), conv(operand1, ins_addr=ins_addr))
    # extension is required
    assert operand1.bits < operand0.bits
    operand1 = ailment.Expr.Convert(None, operand1.bits, operand0.bits, False, operand1)
    return op(conv(operand0, nobool=True, ins_addr=ins_addr), conv(operand1, nobool=True, ins_addr=ins_addr))


def _dummy_bvs(condition, condition_mapping, name_suffix=""):
    var = claripy.BVS(f"ailexpr_{condition!r}{name_suffix}", condition.bits, explicit_name=True)
    condition_mapping[var.args[0]] = condition
    return var


def _dummy_bools(condition, condition_mapping, name_suffix=""):
    var = claripy.BoolS(f"ailexpr_{condition!r}{name_suffix}", explicit_name=True)
    condition_mapping[var.args[0]] = condition
    return var


_ail2claripy_op_mapping = {
    "LogicalAnd": lambda expr, conv, _, ia: claripy.And(
        conv(expr.operands[0], ins_addr=ia), conv(expr.operands[1], ins_addr=ia)
    ),
    "LogicalOr": lambda expr, conv, _, ia: claripy.Or(
        conv(expr.operands[0], ins_addr=ia), conv(expr.operands[1], ins_addr=ia)
    ),
    "CmpEQ": lambda expr, conv, _, ia: conv(expr.operands[0], nobool=True, ins_addr=ia)
    == conv(expr.operands[1], nobool=True, ins_addr=ia),
    "CmpNE": lambda expr, conv, _, ia: conv(expr.operands[0], nobool=True, ins_addr=ia)
    != conv(expr.operands[1], nobool=True, ins_addr=ia),
    "CmpLE": lambda expr, conv, _, ia: conv(expr.operands[0], nobool=True, ins_addr=ia)
    <= conv(expr.operands[1], nobool=True, ins_addr=ia),
    "CmpLE (signed)": lambda expr, conv, _, ia: claripy.SLE(
        conv(expr.operands[0], nobool=True, ins_addr=ia), conv(expr.operands[1], nobool=True, ins_addr=ia)
    ),
    "CmpLT": lambda expr, conv, _, ia: conv(expr.operands[0], nobool=True, ins_addr=ia)
    < conv(expr.operands[1], nobool=True, ins_addr=ia),
    "CmpLT (signed)": lambda expr, conv, _, ia: claripy.SLT(
        conv(expr.operands[0], nobool=True, ins_addr=ia), conv(expr.operands[1], nobool=True, ins_addr=ia)
    ),
    "CmpGE": lambda expr, conv, _, ia: conv(expr.operands[0], nobool=True, ins_addr=ia)
    >= conv(expr.operands[1], nobool=True, ins_addr=ia),
    "CmpGE (signed)": lambda expr, conv, _, ia: claripy.SGE(
        conv(expr.operands[0], nobool=True, ins_addr=ia), conv(expr.operands[1], nobool=True, ins_addr=ia)
    ),
    "CmpGT": lambda expr, conv, _, ia: conv(expr.operands[0], nobool=True, ins_addr=ia)
    > conv(expr.operands[1], nobool=True, ins_addr=ia),
    "CmpGT (signed)": lambda expr, conv, _, ia: claripy.SGT(
        conv(expr.operands[0], nobool=True, ins_addr=ia), conv(expr.operands[1], nobool=True, ins_addr=ia)
    ),
    "CasCmpEQ": lambda expr, conv, _, ia: conv(expr.operands[0], nobool=True, ins_addr=ia)
    == conv(expr.operands[1], nobool=True, ins_addr=ia),
    "CasCmpNE": lambda expr, conv, _, ia: conv(expr.operands[0], nobool=True, ins_addr=ia)
    != conv(expr.operands[1], nobool=True, ins_addr=ia),
    "CasCmpLE": lambda expr, conv, _, ia: conv(expr.operands[0], nobool=True, ins_addr=ia)
    <= conv(expr.operands[1], nobool=True, ins_addr=ia),
    "CasCmpLE (signed)": lambda expr, conv, _, ia: claripy.SLE(
        conv(expr.operands[0], nobool=True, ins_addr=ia), conv(expr.operands[1], nobool=True, ins_addr=ia)
    ),
    "CasCmpLT": lambda expr, conv, _, ia: conv(expr.operands[0], nobool=True, ins_addr=ia)
    < conv(expr.operands[1], nobool=True, ins_addr=ia),
    "CasCmpLT (signed)": lambda expr, conv, _, ia: claripy.SLT(
        conv(expr.operands[0], nobool=True, ins_addr=ia), conv(expr.operands[1], nobool=True, ins_addr=ia)
    ),
    "CasCmpGE": lambda expr, conv, _, ia: conv(expr.operands[0], nobool=True, ins_addr=ia)
    >= conv(expr.operands[1], nobool=True, ins_addr=ia),
    "CasCmpGE (signed)": lambda expr, conv, _, ia: claripy.SGE(
        conv(expr.operands[0], nobool=True, ins_addr=ia), conv(expr.operands[1], nobool=True, ins_addr=ia)
    ),
    "CasCmpGT": lambda expr, conv, _, ia: conv(expr.operands[0], nobool=True, ins_addr=ia)
    > conv(expr.operands[1], nobool=True, ins_addr=ia),
    "CasCmpGT (signed)": lambda expr, conv, _, ia: claripy.SGT(
        conv(expr.operands[0], nobool=True, ins_addr=ia), conv(expr.operands[1], nobool=True, ins_addr=ia)
    ),
    "Add": lambda expr, conv, _, ia: conv(expr.operands[0], nobool=True, ins_addr=ia)
    + conv(expr.operands[1], nobool=True, ins_addr=ia),
    "Sub": lambda expr, conv, _, ia: conv(expr.operands[0], nobool=True, ins_addr=ia)
    - conv(expr.operands[1], nobool=True, ins_addr=ia),
    "Mul": lambda expr, conv, _, ia: conv(expr.operands[0], nobool=True, ins_addr=ia)
    * conv(expr.operands[1], nobool=True, ins_addr=ia),
    "Div": lambda expr, conv, _, ia: conv(expr.operands[0], nobool=True, ins_addr=ia)
    / conv(expr.operands[1], nobool=True, ins_addr=ia),
    "Mod": lambda expr, conv, _, ia: conv(expr.operands[0], nobool=True, ins_addr=ia)
    % conv(expr.operands[1], nobool=True, ins_addr=ia),
    "Not": lambda expr, conv, _, ia: claripy.Not(conv(expr.operand, ins_addr=ia)),
    "Neg": lambda expr, conv, _, ia: -conv(expr.operand, ins_addr=ia),
    "BitwiseNeg": lambda expr, conv, _, ia: ~conv(expr.operand, ins_addr=ia),
    "Xor": lambda expr, conv, _, ia: conv(expr.operands[0], nobool=True, ins_addr=ia)
    ^ conv(expr.operands[1], nobool=True, ins_addr=ia),
    "And": lambda expr, conv, _, ia: conv(expr.operands[0], nobool=True, ins_addr=ia)
    & conv(expr.operands[1], nobool=True, ins_addr=ia),
    "Or": lambda expr, conv, _, ia: conv(expr.operands[0], nobool=True, ins_addr=ia)
    | conv(expr.operands[1], nobool=True, ins_addr=ia),
    "Shr": lambda expr, conv, _, ia: _op_with_unified_size(claripy.LShR, conv, expr.operands[0], expr.operands[1], ia),
    "Shl": lambda expr, conv, _, ia: _op_with_unified_size(
        operator.lshift, conv, expr.operands[0], expr.operands[1], ia
    ),
    "Sar": lambda expr, conv, _, ia: _op_with_unified_size(
        operator.rshift, conv, expr.operands[0], expr.operands[1], ia
    ),
    "Concat": lambda expr, conv, _, ia: claripy.Concat(*[conv(operand, ins_addr=ia) for operand in expr.operands]),
    # There are no corresponding claripy operations for the following operations
    "CmpF": lambda expr, _, m, *args: _dummy_bvs(expr, m),
    "Mull": lambda expr, _, m, *args: _dummy_bvs(expr, m),
    "Mull (signed)": lambda expr, _, m, *args: _dummy_bvs(expr, m),
    "Reinterpret": lambda expr, _, m, *args: _dummy_bvs(expr, m),
    "Rol": lambda expr, _, m, *args: _dummy_bvs(expr, m),
    "Ror": lambda expr, _, m, *args: _dummy_bvs(expr, m),
    "LogicalXor": lambda expr, _, m, *args: _dummy_bvs(expr, m),
    "Carry": lambda expr, _, m, *args: _dummy_bvs(expr, m),
    "SCarry": lambda expr, _, m, *args: _dummy_bvs(expr, m),
    "SBorrow": lambda expr, _, m, *args: _dummy_bvs(expr, m),
    "ExpCmpNE": lambda expr, _, m, *args: _dummy_bools(expr, m),
    "CmpORD": lambda expr, _, m, *args: _dummy_bvs(expr, m),  # in case CmpORDRewriter fails
    "CmpEQV": lambda expr, _, m, *args: _dummy_bvs(expr, m),
    "GetMSBs": lambda expr, _, m, *args: _dummy_bvs(expr, m),
    "ShlNV": lambda expr, _, m, *args: _dummy_bvs(expr, m),
    "ShrNV": lambda expr, _, m, *args: _dummy_bvs(expr, m),
    "InterleaveLOV": lambda expr, _, m, *args: _dummy_bvs(expr, m),
    "InterleaveHIV": lambda expr, _, m, *args: _dummy_bvs(expr, m),
    # catch-all
    "_DUMMY_": lambda expr, _, m, *args: _dummy_bvs(expr, m),
}

#
# The ConditionProcessor class
#


class ConditionProcessor:
    """
    Convert between claripy AST and AIL expressions. Also calculates reaching conditions of all nodes on a graph.
    """

    def __init__(self, arch, condition_mapping=None):
        self.arch = arch
        self._condition_mapping: dict[str, Any] = {} if condition_mapping is None else condition_mapping
        self.jump_table_conds: dict[int, set] = defaultdict(set)
        self.reaching_conditions = {}
        self.guarding_conditions = {}
        self._ast2annotations = {}

        self._peephole_expr_optimizations = [
            cls(None, None, None) for cls in [InvertNegatedLogicalConjunctionsAndDisjunctions, RemoveRedundantNots]
        ]

    def clear(self):
        self._condition_mapping = {}
        self.jump_table_conds = defaultdict(set)
        self.reaching_conditions = {}
        self.guarding_conditions = {}
        self._ast2annotations = {}

    def have_opposite_edge_conditions(self, graph: networkx.DiGraph, src, dst0, dst1) -> bool:
        """
        Check if the edge conditions of two edges (src, dst0) and (src, dst1) are opposite to each other. Try to avoid
        condition translation if possible.
        """

        if src in graph and graph.out_degree[src] == 2 and graph.has_edge(src, dst0) and graph.has_edge(src, dst1):
            # sometimes the last statement is the conditional jump. sometimes it's the first statement of the block
            if isinstance(src, ailment.Block) and src.statements and is_head_controlled_loop_block(src):
                last_stmt = next(
                    iter(stmt for stmt in src.statements[:-1] if isinstance(stmt, ailment.Stmt.ConditionalJump)), None
                )
                assert last_stmt is not None
            else:
                last_stmt = self.get_last_statement(src)

            if isinstance(last_stmt, ailment.Stmt.ConditionalJump):
                return True

        # fallback
        edge_cond_left = self.recover_edge_condition(graph, src, dst0)
        edge_cond_right = self.recover_edge_condition(graph, src, dst1)
        return claripy.is_true(claripy.Not(edge_cond_left) == edge_cond_right)  # type: ignore

    def recover_edge_condition(self, graph: networkx.DiGraph, src, dst):
        edge = src, dst
        edge_data = graph.get_edge_data(*edge)
        edge_type = edge_data.get("type", "transition") if edge_data is not None else "transition"
        try:
            predicate = self._extract_predicate(src, dst, edge_type)
        except EmptyBlockNotice:
            # catch empty block notice - although this should not really happen
            predicate = claripy.true()
        return predicate

    def recover_edge_conditions(self, region, graph=None) -> dict:
        edge_conditions = {}
        # traverse the graph to recover the condition for each edge
        graph = graph or region.graph
        for src in graph.nodes():
            nodes = list(graph[src])
            if len(nodes) >= 1:
                for dst in nodes:
                    predicate = self.recover_edge_condition(graph, src, dst)
                    edge_conditions[(src, dst)] = predicate

        return edge_conditions

    def recover_reaching_conditions(
        self,
        region,
        graph=None,
        with_successors=False,
        case_entry_to_switch_head: dict[int, int] | None = None,
        simplify_conditions: bool = True,
    ):
        """
        Recover the reaching conditions for each block in an acyclic graph. Note that we assume the graph that's passed
        in is acyclic.
        """

        def _strictly_postdominates(inv_idoms, node_a, node_b):
            """
            Does node A strictly post-dominate node B on the graph?
            """
            return dominates(inv_idoms, node_a, node_b)

        edge_conditions = self.recover_edge_conditions(region, graph=graph)

        if graph:
            _g = graph
            head = next(node for node in graph.nodes if graph.in_degree(node) == 0)
        else:
            if with_successors and region.graph_with_successors is not None:
                _g = region.graph_with_successors
            else:
                _g = region.graph
            head = region.head

        # special handling for jump table entries - do not allow crossing between cases
        if case_entry_to_switch_head:
            _g = self._remove_crossing_edges_between_cases(_g, case_entry_to_switch_head)

        inverted_graph, idoms = inverted_idoms(_g)

        reaching_conditions = {}
        # recover the reaching condition for each node
        sorted_nodes = GraphUtils.quasi_topological_sort_nodes(_g)
        terminating_nodes = []
        for node in sorted_nodes:
            # create special conditions for all nodes that are jump table entries
            if case_entry_to_switch_head and node.addr in case_entry_to_switch_head:
                jump_target_var = self.create_jump_target_var(case_entry_to_switch_head[node.addr])
                cond = jump_target_var == claripy.BVV(node.addr, self.arch.bits)
                reaching_conditions[node] = cond
                self.jump_table_conds[case_entry_to_switch_head[node.addr]].add(cond)
                continue

            preds = _g.predecessors(node)
            reaching_condition = None

            out_degree = _g.out_degree(node)
            if out_degree == 0:
                terminating_nodes.append(node)

            if node is head:
                # the head is always reachable
                reaching_condition = claripy.true()
            elif idoms is not None and _strictly_postdominates(idoms, node, head):
                # the node that post dominates the head is always reachable
                reaching_conditions[node] = claripy.true()
            else:
                for pred in preds:
                    edge = (pred, node)
                    pred_condition = reaching_conditions.get(pred, claripy.true())
                    edge_condition = edge_conditions.get(edge, claripy.true())

                    if reaching_condition is None:
                        reaching_condition = claripy.And(pred_condition, edge_condition)
                    else:
                        reaching_condition = claripy.Or(claripy.And(pred_condition, edge_condition), reaching_condition)

            if reaching_condition is not None:
                reaching_conditions[node] = (
                    self.simplify_condition(reaching_condition) if simplify_conditions else reaching_condition
                )

        # My hypothesis: for nodes where two paths come together *and* those that cannot be further structured into
        # another if-else construct (we take the short-cut by testing if the operator is an "Or" after running our
        # condition simplifiers previously), we are better off using their "guarding conditions" instead of their
        # reaching conditions for if-else. see my super long chatlog with rhelmot on 5/14/2021.
        guarding_conditions = {}
        for the_node in sorted_nodes:
            preds = list(_g.predecessors(the_node))
            if len(preds) != 2:
                continue
            # generate a graph slice that goes from the region head to this node
            slice_nodes = list(networkx.dfs_tree(inverted_graph, the_node))
            subgraph = networkx.subgraph(_g, slice_nodes)
            # figure out which paths cause the divergence from this node
            nodes_do_not_reach_the_node = set()
            for node_ in subgraph:
                if node_ is the_node:
                    continue
                for succ in _g.successors(node_):
                    if not networkx.has_path(_g, succ, the_node):
                        nodes_do_not_reach_the_node.add(succ)

            diverging_conditions = []

            for node_ in nodes_do_not_reach_the_node:
                preds_ = list(_g.predecessors(node_))
                for pred_ in preds_:
                    if pred_ in nodes_do_not_reach_the_node:
                        continue
                    # this predecessor is the diverging node!
                    edge_ = pred_, node_
                    edge_condition = edge_conditions.get(edge_, None)
                    if edge_condition is not None:
                        diverging_conditions.append(edge_condition)

            if diverging_conditions:
                # the negation of the union of diverging conditions is the guarding condition for this node
                cond = claripy.Or(*map(claripy.Not, diverging_conditions))  # pylint:disable=bad-builtin
                guarding_conditions[the_node] = cond

        self.reaching_conditions = reaching_conditions
        self.guarding_conditions = guarding_conditions

    def remove_claripy_bool_asts(self, node, memo=None):
        # Convert claripy Bool ASTs to AIL expressions

        if memo is None:
            memo = {}

        if isinstance(node, SequenceNode):
            new_nodes = []
            for n in node.nodes:
                new_node = self.remove_claripy_bool_asts(n, memo=memo)
                new_nodes.append(new_node)
            return SequenceNode(node.addr, new_nodes)

        if isinstance(node, MultiNode):
            new_nodes = []
            for n in node.nodes:
                new_node = self.remove_claripy_bool_asts(n, memo=memo)
                new_nodes.append(new_node)
            return MultiNode(nodes=new_nodes)

        if isinstance(node, CodeNode):
            return CodeNode(
                self.remove_claripy_bool_asts(node.node, memo=memo),
                (
                    None
                    if node.reaching_condition is None
                    else self.convert_claripy_bool_ast(node.reaching_condition, memo=memo)
                ),
            )

        if isinstance(node, ConditionalBreakNode):
            return ConditionalBreakNode(
                node.addr,
                self.convert_claripy_bool_ast(node.condition, memo=memo),
                node.target,
            )

        if isinstance(node, ConditionNode):
            return ConditionNode(
                node.addr,
                (
                    None
                    if node.reaching_condition is None
                    else self.convert_claripy_bool_ast(node.reaching_condition, memo=memo)
                ),
                self.convert_claripy_bool_ast(node.condition, memo=memo),
                self.remove_claripy_bool_asts(node.true_node, memo=memo),
                self.remove_claripy_bool_asts(node.false_node, memo=memo),
            )

        if isinstance(node, CascadingConditionNode):
            cond_and_nodes = []
            for cond, child_node in node.condition_and_nodes:
                cond_and_nodes.append(
                    (
                        self.convert_claripy_bool_ast(cond, memo=memo),
                        self.remove_claripy_bool_asts(child_node, memo=memo),
                    )
                )
            else_node = None if node.else_node is None else self.remove_claripy_bool_asts(node.else_node, memo=memo)
            return CascadingConditionNode(
                node.addr,
                cond_and_nodes,
                else_node=else_node,
            )

        if isinstance(node, LoopNode):
            result = node.copy()
            result.condition = (
                self.convert_claripy_bool_ast(node.condition, memo=memo) if node.condition is not None else None
            )
            result.sequence_node = self.remove_claripy_bool_asts(node.sequence_node, memo=memo)
            return result

        if isinstance(node, SwitchCaseNode):
            return SwitchCaseNode(
                self.convert_claripy_bool_ast(node.switch_expr, memo=memo),
                OrderedDict(
                    (idx, self.remove_claripy_bool_asts(case_node, memo=memo)) for idx, case_node in node.cases.items()
                ),
                self.remove_claripy_bool_asts(node.default_node, memo=memo),
                addr=node.addr,
            )

        if isinstance(node, IncompleteSwitchCaseNode):
            return IncompleteSwitchCaseNode(
                node.addr,
                self.remove_claripy_bool_asts(node.head, memo=memo),
                [self.remove_claripy_bool_asts(case, memo=memo) for case in node.cases],
            )

        return node

    @classmethod
    def get_last_statement(cls, block):
        """
        This is the buggy version of get_last_statements, because, you know, there can always be more than one last
        statement due to the existence of branching statements (like, If-then-else). All methods using
        get_last_statement() should switch to get_last_statements() and properly handle multiple last statements.
        """
        if type(block) is SequenceNode:
            if block.nodes:
                return cls.get_last_statement(block.nodes[-1])
            raise EmptyBlockNotice
        if type(block) is CodeNode:
            return cls.get_last_statement(block.node)
        if type(block) is ailment.Block:
            if not block.statements:
                raise EmptyBlockNotice
            return block.statements[-1]
        if type(block) is Block:
            raise NotImplementedError
        if type(block) is BlockNode:
            raise NotImplementedError
        if type(block) is MultiNode:
            # get the last node
            for the_block in reversed(block.nodes):
                try:
                    return cls.get_last_statement(the_block)
                except EmptyBlockNotice:
                    continue
            raise EmptyBlockNotice
        if type(block) is LoopNode:
            return cls.get_last_statement(block.sequence_node)
        if type(block) is ConditionalBreakNode:
            return None
        if type(block) is ConditionNode:
            s = None
            if block.true_node:
                try:
                    s = cls.get_last_statement(block.true_node)
                except EmptyBlockNotice:
                    s = None
            if s is None and block.false_node:
                s = cls.get_last_statement(block.false_node)
            return s
        if type(block) is CascadingConditionNode:
            s = None
            if block.else_node is not None:
                s = cls.get_last_statement(block.else_node)
            else:
                for _, node in reversed(block.condition_and_nodes):
                    s = cls.get_last_statement(node)
                    if s is not None:
                        break
            return s
        if type(block) is BreakNode:
            return None
        if type(block) is ContinueNode:
            return None
        if type(block) is SwitchCaseNode:
            return None
        if type(block) is IncompleteSwitchCaseNode:
            return None
        if type(block) is GraphRegion:
            # normally this should not happen. however, we have test cases that trigger this case.
            return None

        raise NotImplementedError

    @classmethod
    def get_last_statements(
        cls, block
    ) -> list[ailment.Stmt.Statement | ConditionalBreakNode | BreakNode | ContinueNode | None]:
        if type(block) is SequenceNode:
            for last_node in reversed(block.nodes):
                try:
                    return cls.get_last_statements(last_node)
                except EmptyBlockNotice:
                    # the node is empty. try the next one
                    continue

            raise EmptyBlockNotice

        if type(block) is CodeNode:
            return cls.get_last_statements(block.node)
        if type(block) is ailment.Block:
            if not block.statements:
                raise EmptyBlockNotice
            return [block.statements[-1]]
        if type(block) is Block:
            raise NotImplementedError
        if type(block) is BlockNode:
            raise NotImplementedError
        if type(block) is MultiNode:
            # get the last node
            for the_block in reversed(block.nodes):
                try:
                    return cls.get_last_statements(the_block)
                except EmptyBlockNotice:
                    continue
            raise EmptyBlockNotice
        if type(block) is LoopNode:
            if block.sequence_node is None:
                raise EmptyBlockNotice
            return cls.get_last_statements(block.sequence_node)
        if type(block) is ConditionalBreakNode:
            return [block]
        if type(block) is ConditionNode:
            s = []
            if block.true_node:
                try:
                    last_stmts = cls.get_last_statements(block.true_node)
                    s.extend(last_stmts)
                except EmptyBlockNotice:
                    pass
            else:
                s.append(None)
            if block.false_node:
                last_stmts = cls.get_last_statements(block.false_node)
                s.extend(last_stmts)
            else:
                s.append(None)
            return s
        if type(block) is CascadingConditionNode:
            s = []
            if block.else_node is not None:
                try:
                    last_stmts = cls.get_last_statements(block.else_node)
                    s.extend(last_stmts)
                except EmptyBlockNotice:
                    pass
            else:
                s.append(None)
            for _, node in block.condition_and_nodes:
                last_stmts = cls.get_last_statements(node)
                s.extend(last_stmts)
            return s
        if type(block) is BreakNode:
            return [block]
        if type(block) is ContinueNode:
            return [block]
        if type(block) is SwitchCaseNode:
            s = []
            for case in block.cases.values():
                s.extend(cls.get_last_statements(case))
            if block.default_node is not None:
                s.extend(cls.get_last_statements(block.default_node))
            else:
                s.append(None)
            return s
        if type(block) is IncompleteSwitchCaseNode:
            s = []
            for case in block.cases:
                s.extend(cls.get_last_statements(case))
            return s
        if type(block) is GraphRegion:
            # normally this should not happen. however, we have test cases that trigger this case.
            return []

        raise NotImplementedError

    #
    # Path predicate
    #

    EXC_COUNTER = 1000

    def _extract_predicate(self, src_block, dst_block, edge_type) -> claripy.ast.Bool:
        if edge_type == "exception":
            # TODO: THIS IS ABSOLUTELY A HACK. AT THIS MOMENT YOU SHOULD NOT ATTEMPT TO MAKE SENSE OF EXCEPTION EDGES.
            self.EXC_COUNTER += 1
            return self.claripy_ast_from_ail_condition(
                ailment.Expr.BinaryOp(
                    None,
                    "CmpEQ",
                    (
                        ailment.Expr.Register(0x400000 + self.EXC_COUNTER, None, self.EXC_COUNTER, 64),
                        ailment.Expr.Const(None, None, self.EXC_COUNTER, 64),
                    ),
                    False,
                ),
                ins_addr=dst_block.addr,
            )

        if type(src_block) is ConditionalBreakNode:
            # at this point ConditionalBreakNode stores a claripy AST
            bool_var = src_block.condition
            if src_block.target == dst_block.addr:
                return bool_var
            return claripy.Not(bool_var)

        if type(src_block) is GraphRegion:
            return claripy.true()

        # sometimes the last statement is the conditional jump. sometimes it's the first statement of the block
        if isinstance(src_block, ailment.Block) and src_block.statements and is_head_controlled_loop_block(src_block):
            last_stmt = next(
                iter(stmt for stmt in src_block.statements[:-1] if isinstance(stmt, ailment.Stmt.ConditionalJump)), None
            )
            assert last_stmt is not None
        else:
            last_stmt = self.get_last_statement(src_block)

        if last_stmt is None:
            return claripy.true()
        if type(last_stmt) is ailment.Stmt.Jump:
            if isinstance(last_stmt.target, ailment.Expr.Const):
                return claripy.true()
            # indirect jump
            target_ast = self.claripy_ast_from_ail_condition(last_stmt.target, ins_addr=last_stmt.ins_addr)
            return target_ast == dst_block.addr
        if type(last_stmt) is ailment.Stmt.ConditionalJump:
            bool_var = self.claripy_ast_from_ail_condition(
                last_stmt.condition, must_bool=True, ins_addr=last_stmt.ins_addr
            )
            if isinstance(last_stmt.true_target, ailment.Expr.Const) and last_stmt.true_target.value == dst_block.addr:
                return bool_var
            return claripy.Not(bool_var)

        return claripy.true()

    #
    # Expression conversion
    #

    def _convert_extract(self, hi, lo, expr, tags, memo=None):
        # ailment does not support Extract. We translate Extract to Convert and shift.
        if lo == 0:
            return ailment.Expr.Convert(
                None,
                expr.size(),
                hi + 1,
                False,
                self.convert_claripy_bool_ast(expr, memo=memo),
                **tags,
            )

        raise NotImplementedError("This case will be implemented once encountered.")

    def convert_claripy_bool_ast(self, cond, memo=None):
        """
        Convert recovered reaching conditions from claripy ASTs to ailment Expressions

        :return: None
        """

        if not isinstance(cond, claripy.ast.Base):
            return cond
        if memo is None:
            memo = {}
        if cond._hash in memo:
            return memo[cond._hash]
        r = self.convert_claripy_bool_ast_core(cond, memo)
        optimized_r = peephole_optimize_expr(r, self._peephole_expr_optimizations)
        r = r if optimized_r is None else optimized_r
        memo[cond._hash] = r
        return r

    def convert_claripy_bool_ast_core(self, cond, memo):
        if isinstance(cond, ailment.Expr.Expression):
            return cond

        if cond.op in {"BoolS", "BoolV"} and claripy.is_true(cond):
            return ailment.Expr.Const(None, None, True, 1)
        if cond in self._condition_mapping:
            return self._condition_mapping[cond]
        if cond.op in {"BVS", "BoolS"} and cond.args[0] in self._condition_mapping:
            return self._condition_mapping[cond.args[0]]

        def _binary_op_reduce(op, args, tags, signed=False):
            r = None
            for arg in args:
                if r is None:
                    r = self.convert_claripy_bool_ast(arg, memo=memo)
                else:
                    r = ailment.Expr.BinaryOp(
                        None, op, (r, self.convert_claripy_bool_ast(arg, memo=memo)), signed, **tags
                    )
            return r

        def _unary_op_reduce(op, arg, tags):
            r = self.convert_claripy_bool_ast(arg, memo=memo)
            # TODO: Keep track of tags
            return ailment.Expr.UnaryOp(None, op, r, **tags)

        _mapping = {
            "Not": lambda cond_, tags: _unary_op_reduce("Not", cond_.args[0], tags),
            "__neg__": lambda cond_, tags: _unary_op_reduce("Not", cond_.args[0], tags),
            "__invert__": lambda cond_, tags: _unary_op_reduce("BitwiseNeg", cond_.args[0], tags),
            "And": lambda cond_, tags: _binary_op_reduce("LogicalAnd", cond_.args, tags),
            "Or": lambda cond_, tags: _binary_op_reduce("LogicalOr", cond_.args, tags),
            "__le__": lambda cond_, tags: _binary_op_reduce("CmpLE", cond_.args, tags, signed=True),
            "SLE": lambda cond_, tags: _binary_op_reduce("CmpLE", cond_.args, tags, signed=True),
            "__lt__": lambda cond_, tags: _binary_op_reduce("CmpLT", cond_.args, tags, signed=True),
            "SLT": lambda cond_, tags: _binary_op_reduce("CmpLT", cond_.args, tags, signed=True),
            "UGT": lambda cond_, tags: _binary_op_reduce("CmpGT", cond_.args, tags),
            "UGE": lambda cond_, tags: _binary_op_reduce("CmpGE", cond_.args, tags),
            "__gt__": lambda cond_, tags: _binary_op_reduce("CmpGT", cond_.args, tags, signed=True),
            "__ge__": lambda cond_, tags: _binary_op_reduce("CmpGE", cond_.args, tags, signed=True),
            "SGT": lambda cond_, tags: _binary_op_reduce("CmpGT", cond_.args, tags, signed=True),
            "SGE": lambda cond_, tags: _binary_op_reduce("CmpGE", cond_.args, tags, signed=True),
            "ULT": lambda cond_, tags: _binary_op_reduce("CmpLT", cond_.args, tags),
            "ULE": lambda cond_, tags: _binary_op_reduce("CmpLE", cond_.args, tags),
            "__eq__": lambda cond_, tags: _binary_op_reduce("CmpEQ", cond_.args, tags),
            "__ne__": lambda cond_, tags: _binary_op_reduce("CmpNE", cond_.args, tags),
            "__add__": lambda cond_, tags: _binary_op_reduce("Add", cond_.args, tags, signed=False),
            "__sub__": lambda cond_, tags: _binary_op_reduce("Sub", cond_.args, tags),
            "__mul__": lambda cond_, tags: _binary_op_reduce("Mul", cond_.args, tags),
            "__xor__": lambda cond_, tags: _binary_op_reduce("Xor", cond_.args, tags),
            "__or__": lambda cond_, tags: _binary_op_reduce("Or", cond_.args, tags, signed=False),
            "__and__": lambda cond_, tags: _binary_op_reduce("And", cond_.args, tags),
            "__lshift__": lambda cond_, tags: _binary_op_reduce("Shl", cond_.args, tags),
            "__rshift__": lambda cond_, tags: _binary_op_reduce("Sar", cond_.args, tags),
            "__floordiv__": lambda cond_, tags: _binary_op_reduce("Div", cond_.args, tags),
            "__mod__": lambda cond_, tags: _binary_op_reduce("Mod", cond_.args, tags),
            "LShR": lambda cond_, tags: _binary_op_reduce("Shr", cond_.args, tags),
            "BVV": lambda cond_, tags: ailment.Expr.Const(None, None, cond_.args[0], cond_.size(), **tags),
            "BoolV": lambda cond_, tags: (
                ailment.Expr.Const(None, None, True, 1, **tags)
                if cond_.args[0] is True
                else ailment.Expr.Const(None, None, False, 1, **tags)
            ),
            "Extract": lambda cond_, tags: self._convert_extract(*cond_.args, tags, memo=memo),
            "ZeroExt": lambda cond_, tags: _binary_op_reduce(
                "Concat", [claripy.BVV(0, cond_.args[0]), cond_.args[1]], tags
            ),
            "Concat": lambda cond_, tags: _binary_op_reduce("Concat", cond_.args, tags),
        }

        if cond.op in _mapping:
            if cond in self._ast2annotations:
                cond_tags = self._ast2annotations.get(cond)
            elif claripy.Not(cond) in self._ast2annotations:
                cond_tags = self._ast2annotations.get(claripy.Not(cond))
            else:
                cond_tags = {}
            return _mapping[cond.op](cond, cond_tags)
        raise NotImplementedError(
            f"Condition variable {cond} has an unsupported operator {cond.op}. Consider implementing."
        )

    def claripy_ast_from_ail_condition(
        self, condition, *, nobool: bool = False, must_bool: bool = False, ins_addr: int = 0
    ) -> claripy.ast.Bool | claripy.ast.Bits:
        # Unpack a condition all the way to the leaves
        if isinstance(
            condition, (claripy.ast.Bits, claripy.ast.Bool)
        ):  # pylint:disable=isinstance-second-argument-not-valid-type
            return condition

        if isinstance(
            condition,
            (ailment.Expr.VEXCCallExpression, ailment.Expr.BasePointerOffset, ailment.Expr.ITE),
        ):
            return _dummy_bvs(condition, self._condition_mapping)
        if isinstance(condition, ailment.Stmt.Call):
            return _dummy_bvs(condition, self._condition_mapping, name_suffix=hex(condition.tags.get("ins_addr", 0)))
        if isinstance(condition, (ailment.Expr.Load, ailment.Expr.Register, ailment.Expr.VirtualVariable)):
            # does it have a variable associated?
            if condition.variable is not None:
                var = claripy.BVS(
                    f"ailexpr_{condition!r}-{condition.variable.ident}-{ins_addr:x}",
                    condition.bits,
                    explicit_name=True,
                )
            else:
                var = claripy.BVS(
                    f"ailexpr_{condition!r}-{condition.idx}-{ins_addr:x}", condition.bits, explicit_name=True
                )
            self._condition_mapping[var.args[0]] = condition
            return var
        if isinstance(condition, ailment.Expr.Convert):
            # convert is special. if it generates a 1-bit variable, it should be treated as a BoolS
            if condition.to_bits == 1 and not nobool:
                var_ = self.claripy_ast_from_ail_condition(condition.operands[0], ins_addr=ins_addr)
                name = f"ailcond_Conv({condition.from_bits}->{condition.to_bits}, {hash(var_)})"
                var = claripy.BoolS(name, explicit_name=True)
            else:
                var_ = self.claripy_ast_from_ail_condition(condition.operands[0], ins_addr=ins_addr)
                name = f"ailexpr_Conv({condition.from_bits}->{condition.to_bits}, {hash(var_)})"
                var = claripy.BVS(name, condition.to_bits, explicit_name=True)
            self._condition_mapping[var.args[0]] = condition
            return var
        if isinstance(condition, ailment.Expr.Const):
            if condition.value is True or condition.value is False:
                var = claripy.BoolV(condition.value)
            else:
                var = claripy.BVV(condition.value, condition.bits)
            if isinstance(var, claripy.ast.Bits) and var.size() == 1:
                var = claripy.true() if var.concrete_value == 1 else claripy.false()
            return var
        if isinstance(condition, ailment.Expr.Tmp):
            l.warning("Left-over ailment.Tmp variable %s.", condition)
            if condition.bits == 1:
                var = claripy.BoolS(f"ailtmp_{condition.tmp_idx}", explicit_name=True)
            else:
                var = claripy.BVS(f"ailtmp_{condition.tmp_idx}", condition.bits, explicit_name=True)
            self._condition_mapping[var.args[0]] = condition
            return var
        if isinstance(condition, ailment.Expr.MultiStatementExpression):
            # just cache it
            if condition.bits == 1:
                var = claripy.BoolS(f"mstmtexpr_{hash(condition)}", explicit_name=True)
            else:
                var = claripy.BVS(f"mstmtexpr_{hash(condition)}", condition.bits, explicit_name=True)
            self._condition_mapping[var.args[0]] = condition
            return var

        lambda_expr = _ail2claripy_op_mapping.get(condition.verbose_op, None)
        if lambda_expr is None:
            # fall back to op
            lambda_expr = _ail2claripy_op_mapping.get(condition.op, None)
        if lambda_expr is None:
            # fall back to the catch-all option
            l.debug(
                "Unsupported AIL expression operation %s (or verbose: %s). Fall back to the default catch-all dummy "
                "option. Consider implementing.",
                condition.op,
                condition.verbose_op,
            )
            lambda_expr = _ail2claripy_op_mapping["_DUMMY_"]
        r = lambda_expr(condition, self.claripy_ast_from_ail_condition, self._condition_mapping, ins_addr)

        if isinstance(r, claripy.ast.Bool) and nobool:
            r = claripy.BVS(f"ailexpr_from_bool_{r!r}", 1, explicit_name=True)
            self._condition_mapping[r.args[0]] = condition

        if r is NotImplemented:
            if condition.bits == 1 and not nobool:
                r = claripy.BoolS(f"ailexpr_{condition!r}", explicit_name=True)
            else:
                r = claripy.BVS(f"ailexpr_{condition!r}", condition.bits, explicit_name=True)
            self._condition_mapping[r.args[0]] = condition
        # don't lose tags
        self._ast2annotations[r] = condition.tags

        if isinstance(r, claripy.ast.BV) and r.size() == 1 and must_bool:
            # convert to a BoolS
            if r.op == "BVV":
                r = claripy.false() if r.args[0] == 0 else claripy.true()
            else:
                # r.op == "BVS"
                r = claripy.BoolS(f"bool_from_bv1_{r.args[0]}", explicit_name=True)
                self._condition_mapping[r.args[0]] = condition
        return r

    #
    # Expression simplification
    #

    @staticmethod
    def claripy_ast_to_sympy_expr(ast, memo=None):

        import sympy  # pylint:disable=import-outside-toplevel

        if ast.op == "And":
            return sympy.And(*(ConditionProcessor.claripy_ast_to_sympy_expr(arg, memo=memo) for arg in ast.args))
        if ast.op == "Or":
            return sympy.Or(*(ConditionProcessor.claripy_ast_to_sympy_expr(arg, memo=memo) for arg in ast.args))
        if ast.op == "Not":
            return sympy.Not(ConditionProcessor.claripy_ast_to_sympy_expr(ast.args[0], memo=memo))

        if ast.op in _UNIFIABLE_COMPARISONS:
            # unify comparisons to enable more simplification opportunities without going "deep" in sympy
            inverse_op = getattr(ast.args[0], _INVERSE_OPERATIONS[ast.op])
            return sympy.Not(ConditionProcessor.claripy_ast_to_sympy_expr(inverse_op(ast.args[1]), memo=memo))

        if memo is not None and ast in memo:
            return memo[ast]
        symbol = sympy.Symbol(str(hash(ast)))
        if memo is not None:
            memo[symbol] = ast
        return symbol

    @staticmethod
    def sympy_expr_to_claripy_ast(expr, memo: dict):

        import sympy  # pylint:disable=import-outside-toplevel

        if expr.is_Symbol:
            return memo[expr]
        if isinstance(expr, sympy.Or):
            return claripy.Or(*(ConditionProcessor.sympy_expr_to_claripy_ast(arg, memo) for arg in expr.args))
        if isinstance(expr, sympy.And):
            return claripy.And(*(ConditionProcessor.sympy_expr_to_claripy_ast(arg, memo) for arg in expr.args))
        if isinstance(expr, sympy.Not):
            return claripy.Not(ConditionProcessor.sympy_expr_to_claripy_ast(expr.args[0], memo))
        if isinstance(expr, sympy.logic.boolalg.BooleanTrue):
            return claripy.true()
        if isinstance(expr, sympy.logic.boolalg.BooleanFalse):
            return claripy.false()
        raise AngrRuntimeError("Unreachable reached")

    @staticmethod
    def simplify_condition(cond, depth_limit=8, variables_limit=8):

        import sympy  # pylint:disable=import-outside-toplevel

        memo = {}
        if cond.depth > depth_limit or len(cond.variables) > variables_limit:
            return cond
        sympy_expr = ConditionProcessor.claripy_ast_to_sympy_expr(cond, memo=memo)
        return ConditionProcessor.sympy_expr_to_claripy_ast(sympy.simplify_logic(sympy_expr, deep=False), memo)

    @staticmethod
    def _simplify_trivial_cases(cond):
        if cond.op == "And":
            new_args = []
            for arg in cond.args:
                claripy_simplified = claripy.simplify(arg)
                if claripy.is_true(claripy_simplified):
                    continue
                new_args.append(arg)
            return claripy.And(*new_args)

        return None

    @staticmethod
    def _revert_short_circuit_conditions(cond):
        # revert short-circuit conditions
        # !A||(A&&!B) ==> !(A&&B)

        if cond.op != "Or":
            return cond

        if len(cond.args) == 1:
            # redundant operator. get rid of it
            return cond.args[0]

        or_arg0, or_arg1 = cond.args[:2]
        if or_arg1.op == "And":
            pass
        elif or_arg0.op == "And":
            or_arg0, or_arg1 = or_arg1, or_arg0
        else:
            return cond

        not_a = or_arg0
        solver = claripy.SolverCacheless()

        if not_a.variables == or_arg1.args[0].variables:
            solver.add(not_a == or_arg1.args[0])
            not_b = or_arg1.args[1]
        elif not_a.variables == or_arg1.args[1].variables:
            solver.add(not_a == or_arg1.args[1])
            not_b = or_arg1.args[0]
        else:
            return cond

        if not solver.satisfiable():
            # found it!
            b = claripy.Not(not_b)
            a = claripy.Not(not_a)
            if len(cond.args) <= 2:
                return claripy.Not(claripy.And(a, b))
            return claripy.Or(claripy.Not(claripy.And(a, b)), *cond.args[2:])
        return cond

    @staticmethod
    def _fold_double_negations(cond):
        # !(!A) ==> A
        # !((!A) && (!B)) ==> A || B
        # !((!A) && B) ==> A || !B
        # !(A || B) ==> (!A && !B)

        if cond.op != "Not":
            return None
        if cond.args[0].op == "Not":
            return cond.args[0]

        if cond.args[0].op == "And" and len(cond.args[0].args) == 2:
            and_0, and_1 = cond.args[0].args
            if and_0.op == "Not" and and_1.op == "Not":
                return claripy.Or(and_0.args[0], and_1.args[0])

            if and_0.op == "Not":  # and_1.op != "Not"
                return claripy.Or(and_0.args[0], ConditionProcessor.simplify_condition(claripy.Not(and_1)))

        if cond.args[0].op == "Or" and len(cond.args[0].args) == 2:
            or_0, or_1 = cond.args[0].args
            return claripy.And(
                ConditionProcessor.simplify_condition(claripy.Not(or_0)),
                ConditionProcessor.simplify_condition(claripy.Not(or_1)),
            )

        return None

    @staticmethod
    def _extract_common_subexpressions(cond):
        def _expr_inside_collection(expr_, coll_) -> bool:
            return any(expr_ is ex_ for ex_ in coll_)

        # (A && B) || (A && C) => A && (B || C)
        if cond.op == "And":
            args = [ConditionProcessor._extract_common_subexpressions(arg) for arg in cond.args]
            if all(arg is None for arg in args):
                return None
            return claripy.And(*((arg if arg is not None else ori_arg) for arg, ori_arg in zip(args, cond.args)))

        if cond.op == "Or":
            args = [ConditionProcessor._extract_common_subexpressions(arg) for arg in cond.args]
            args = [(arg if arg is not None else ori_arg) for arg, ori_arg in zip(args, cond.args)]

            expr_ctrs = defaultdict(int)
            for arg in args:
                if arg.op == "And":
                    for subexpr in arg.args:
                        expr_ctrs[subexpr] += 1
                else:
                    expr_ctrs[arg] += 1

            common_exprs = []
            for expr, ctr in expr_ctrs.items():
                if ctr == len(args):
                    # found a common one
                    common_exprs.append(expr)

            if not common_exprs:
                return claripy.Or(*args)

            new_args = []
            for arg in args:
                if arg.op == "And":
                    new_subexprs = [
                        subexpr for subexpr in arg.args if not _expr_inside_collection(subexpr, common_exprs)
                    ]
                    new_args.append(claripy.And(*new_subexprs))
                elif arg in common_exprs:
                    continue
                else:
                    raise AngrRuntimeError("Unexpected behavior - you should never reach here")

            return claripy.And(*common_exprs, claripy.Or(*new_args))

        return None

    @staticmethod
    def _extract_terms(ast: claripy.ast.Bool) -> Generator[claripy.ast.Bool]:
        if ast.op == "And" or ast.op == "Or":
            for arg in ast.args:
                yield from ConditionProcessor._extract_terms(arg)
        elif ast.op == "Not":
            yield from ConditionProcessor._extract_terms(ast.args[0])
        else:
            yield ast

    @staticmethod
    def _replace_term_in_ast(
        ast: claripy.ast.Bool,
        r0: claripy.ast.Bool,
        r0_with: claripy.ast.Bool,
        r1: claripy.ast.Bool,
        r1_with: claripy.ast.Bool,
    ) -> claripy.ast.Bool:
        if ast.op == "And":
            return claripy.And(
                *(ConditionProcessor._replace_term_in_ast(arg, r0, r0_with, r1, r1_with) for arg in ast.args)
            )
        if ast.op == "Or":
            return claripy.Or(
                *(ConditionProcessor._replace_term_in_ast(arg, r0, r0_with, r1, r1_with) for arg in ast.args)
            )
        if ast.op == "Not":
            return claripy.Not(ConditionProcessor._replace_term_in_ast(ast.args[0], r0, r0_with, r1, r1_with))
        if ast is r0:
            return r0_with
        if ast is r1:
            return r1_with
        return ast

    @staticmethod
    def _remove_redundant_terms(cond):
        """
        Extract all terms and test for each term if its truism impacts the truism of the entire condition. If not, the
        term is redundant and can be replaced with a True.
        """

        all_terms = set()
        for term in ConditionProcessor._extract_terms(cond):
            if term not in all_terms:
                all_terms.add(term)

        negations = {}
        to_skip = set()
        all_terms_without_negs = set()
        for term in all_terms:
            if term in to_skip:
                continue
            neg = claripy.Not(term)
            if neg in all_terms:
                negations[term] = neg
                to_skip.add(neg)
                all_terms_without_negs.add(term)
            else:
                all_terms_without_negs.add(term)

        solver = claripy.SolverCacheless()
        for term in all_terms_without_negs:
            neg = negations.get(term)

            replaced_with_true = ConditionProcessor._replace_term_in_ast(
                cond, term, claripy.true(), neg, claripy.false()
            )
            sat0 = solver.satisfiable(
                extra_constraints=(
                    cond,
                    claripy.Not(replaced_with_true),
                )
            )
            sat1 = solver.satisfiable(
                extra_constraints=(
                    claripy.Not(cond),
                    replaced_with_true,
                )
            )
            if sat0 or sat1:
                continue

            replaced_with_false = ConditionProcessor._replace_term_in_ast(
                cond, term, claripy.false(), neg, claripy.true()
            )
            sat0 = solver.satisfiable(
                extra_constraints=(
                    cond,
                    claripy.Not(replaced_with_false),
                )
            )
            sat1 = solver.satisfiable(
                extra_constraints=(
                    claripy.Not(cond),
                    replaced_with_false,
                )
            )
            if sat0 or sat1:
                continue

            # TODO: Finish the implementation
            print(term, "is redundant")

    #
    # Graph processing
    #

    @staticmethod
    def _remove_crossing_edges_between_cases(
        graph: networkx.DiGraph, case_entry_to_switch_head: dict[int, int]
    ) -> networkx.DiGraph:
        starting_nodes = {node for node in graph if node.addr in case_entry_to_switch_head}
        if not starting_nodes:
            return graph

        traversed_nodes = set()
        edges_to_remove = set()
        for starting_node in starting_nodes:
            queue = [starting_node]
            while queue:
                src = queue.pop(0)
                traversed_nodes.add(src)
                successors = graph.successors(src)
                for succ in successors:
                    if succ in traversed_nodes:
                        # we should not traverse this node twice
                        if graph.out_degree(succ) > 0:
                            edges_to_remove.add((src, succ))
                        continue
                    if succ in starting_nodes:
                        # we do not want any jump from one node to a starting node
                        edges_to_remove.add((src, succ))
                        continue
                    traversed_nodes.add(src)
                    queue.append(succ)

        if not edges_to_remove:
            return graph

        # make a copy before modifying the graph
        graph = networkx.DiGraph(graph)
        graph.remove_edges_from(edges_to_remove)
        return graph

    #
    # Utils
    #

    def create_jump_target_var(self, jumptable_head_addr: int):
        return claripy.BVS(f"jump_table_{jumptable_head_addr:x}", self.arch.bits, explicit_name=True)
