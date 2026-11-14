from nicegui import run, ui

from qed_progress.tree import ICON, scan_tree

# Marks the attempt / revision / proof the pipeline is on right now. The web
# tree has no per-node styling hook, so a label prefix is the cheapest visible
# cue (the TUI uses Rich's [reverse] instead).
LIVE_MARK = "▶ "


class AttemptTree:
    """attempt -> revision -> proof tree, rebuilt from the output directory.

    ``ui.tree`` has no public setter for its node list: ``Tree.nodes`` is an
    iterator *method*, so assigning to it shadows the method and leaves the
    rendered prop untouched. Writing the prop is the only way in.
    """

    def __init__(self, output_dir: str):
        self.output_dir = output_dir
        self._signature = None
        self._expanded_keys = ()
        self.tree = ui.tree([], label_key="label").classes("w-full")
        self.tree.expand()

    async def reload(self, live: dict | None = None) -> None:
        """Rescan the directory and push the tree if anything changed."""
        live = live or {}
        data = await run.io_bound(scan_tree, self.output_dir)
        nodes = self._to_nodes(data, live)
        signature = repr(nodes)
        if signature != self._signature:
            self._signature = signature
            self.tree._props["nodes"] = nodes
            self.tree.update()
        self._expand(live)

    def _to_nodes(self, data, live):
        attempt = live.get("attempt")
        revision = live.get("revision")
        proof = live.get("proof")

        def label(name, status, is_live):
            text = f"{name} [{ICON.get(status, '?')}]"
            return f"{LIVE_MARK}{text}" if is_live else text

        nodes = []
        for a in data:
            a_live = attempt is not None and a["name"] == f"attempt_{attempt}"
            a_node = {
                "id": a["name"],
                "label": label(a["name"], a["status"], a_live),
                "children": [],
            }
            for r in a["revisions"]:
                r_live = (a_live and revision is not None
                          and r["name"] == f"revision_{revision}")
                r_node = {
                    "id": f"{a['name']}/{r['name']}",
                    "label": label(r["name"], r["status"], r_live),
                    "children": [],
                }
                for p in r["proofs"]:
                    p_live = (r_live and proof is not None
                              and p["name"] == f"proof_{proof}")
                    r_node["children"].append({
                        "id": f"{a['name']}/{r['name']}/{p['name']}",
                        "label": label(p["name"], p["status"], p_live),
                    })
                a_node["children"].append(r_node)
            nodes.append(a_node)
        return nodes

    def _expand(self, live):
        """Open the branch the pipeline is on, using real node ids.

        ``expand`` takes node keys, not positions: passing ``"1"`` used to add
        ``"1"`` / ``"1/1"`` to the expanded list, which matches no node in a
        tree keyed by ``attempt_1`` / ``attempt_1/revision_1``.
        """
        attempt = live.get("attempt")
        if attempt is None:
            return
        a_key = f"attempt_{attempt}"
        keys = [a_key]
        revision = live.get("revision")
        if revision is not None:
            r_key = f"{a_key}/revision_{revision}"
            keys.append(r_key)
            proof = live.get("proof")
            if proof is not None:
                keys.append(f"{r_key}/proof_{proof}")
        if tuple(keys) == self._expanded_keys:
            return
        self._expanded_keys = tuple(keys)
        self.tree.expand(keys)
