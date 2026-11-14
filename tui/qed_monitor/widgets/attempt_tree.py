from textual.widgets import Tree

from qed_progress.tree import ICON


class AttemptTree(Tree):
    """attempt → revision → proof 三层树。

    节点按层级路径建索引（``attempt_3``、``attempt_3/revision_1``、
    ``attempt_3/revision_1/proof_2``），高亮时精确查表。早前用 ``str(n) in label``
    判断，``proof=1`` 会同时命中 ``proof_1`` 与 ``proof_10``；高亮直接改写 label
    且从不还原，切换阶段后旧节点一直反显。
    """

    def __init__(self, *args, **kwargs):
        super().__init__("attempts", *args, **kwargs)
        self.show_root = False
        self._last_highlight = None
        self._highlighted = None  # (node, 原始 label)
        self._index = {}

    def load(self, tree_data):
        self.root.remove_children()
        # 树重建后旧节点已脱离视图，必须丢掉引用，否则还原时写回孤立节点。
        self._highlighted = None
        self._last_highlight = None
        self._index = {}
        for a in tree_data:
            a_node = self.root.add(
                f"{a['name']} [{ICON.get(a['status'], '?')}]", expand=True)
            self._index[a["name"]] = a_node
            for r in a["revisions"]:
                r_node = a_node.add(
                    f"{r['name']} [{ICON.get(r['status'], '?')}]")
                # 复合键：同名 revision/proof 会出现在每个父节点下，裸名字会互相覆盖。
                self._index[f"{a['name']}/{r['name']}"] = r_node
                for p in r["proofs"]:
                    p_node = r_node.add(
                        f"{p['name']} [{ICON.get(p['status'], '?')}]")
                    self._index[
                        f"{a['name']}/{r['name']}/{p['name']}"] = p_node
        self.refresh()

    def _restore(self):
        """把上一次高亮的节点恢复成原始 label。"""
        if self._highlighted is None:
            return
        node, label = self._highlighted
        self._highlighted = None
        try:
            node.label = label
        except Exception:
            # 节点已被 load() 重建移除，忽略即可。
            pass

    def highlight(self, live):
        attempt = live.get("attempt")
        if attempt is None:
            return
        key = (attempt, live.get("revision"), live.get("proof"))
        if key == self._last_highlight:
            return
        self._last_highlight = key
        self._restore()

        a_key = f"attempt_{attempt}"
        target = self._index.get(a_key)
        if target is None:
            return
        target.expand()

        revision = live.get("revision")
        if revision is not None:
            r_key = f"{a_key}/revision_{revision}"
            r_node = self._index.get(r_key)
            if r_node is not None:
                r_node.expand()
                target = r_node
                proof = live.get("proof")
                if proof is not None:
                    p_node = self._index.get(f"{r_key}/proof_{proof}")
                    if p_node is not None:
                        target = p_node

        self._highlighted = (target, target.label.plain)
        target.label = f"[reverse]{target.label.plain}[/reverse]"
