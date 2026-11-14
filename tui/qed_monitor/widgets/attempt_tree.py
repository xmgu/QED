from textual.widgets import Tree

from qed_progress.tree import ICON


class AttemptTree(Tree):
    """attempt -> revision -> proof tree.

    Nodes are indexed by their path (``attempt_3``, ``attempt_3/revision_1``,
    ``attempt_3/revision_1/proof_2``) so highlighting is an exact lookup. An
    earlier version tested ``str(n) in label``, which made ``proof=1`` match
    both ``proof_1`` and ``proof_10``; it also rewrote the label without ever
    restoring it, so the old node stayed inverted after the stage moved on.
    """

    def __init__(self, *args, **kwargs):
        super().__init__("attempts", *args, **kwargs)
        self.show_root = False
        self._last_highlight = None
        self._highlighted = None  # (node, original label)
        self._index = {}

    def load(self, tree_data):
        self.root.remove_children()
        # Rebuilding detaches the old nodes; drop the reference or restoring
        # would write the label back onto a node that is no longer displayed.
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
                # Composite key: the same revision/proof name appears under
                # every parent, so a bare name would overwrite its siblings.
                self._index[f"{a['name']}/{r['name']}"] = r_node
                for p in r["proofs"]:
                    p_node = r_node.add(
                        f"{p['name']} [{ICON.get(p['status'], '?')}]")
                    self._index[
                        f"{a['name']}/{r['name']}/{p['name']}"] = p_node
        self.refresh()

    def _restore(self):
        """Restore the previously highlighted node to its original label."""
        if self._highlighted is None:
            return
        node, label = self._highlighted
        self._highlighted = None
        try:
            node.label = label
        except Exception:
            # The node was removed by a load(); nothing left to restore.
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
