from nicegui import ui, run
from qed_progress.tree import scan_tree, ICON


class AttemptTree:
    def __init__(self, output_dir: str):
        self.output_dir = output_dir
        self.tree = ui.tree([], label_key="label").classes("w-full")
        self.tree.expand()

    async def reload(self):
        data = await run.io_bound(scan_tree, self.output_dir)
        self.tree.nodes = self._to_nodes(data)
        self.tree.update()

    def _to_nodes(self, data):
        nodes = []
        for a in data:
            a_node = {
                "id": a["name"],
                "label": f"{a['name']} [{ICON.get(a['status'], '?')}]",
                "children": [],
            }
            for r in a["revisions"]:
                r_node = {
                    "id": f"{a['name']}/{r['name']}",
                    "label": f"{r['name']} [{ICON.get(r['status'], '?')}]",
                    "children": [],
                }
                for p in r["proofs"]:
                    r_node["children"].append({
                        "id": f"{a['name']}/{r['name']}/{p['name']}",
                        "label": f"{p['name']} [{ICON.get(p['status'], '?')}]",
                    })
                a_node["children"].append(r_node)
            nodes.append(a_node)
        return nodes

    def highlight(self, live: dict):
        if not live:
            return
        attempt = live.get("attempt")
        revision = live.get("revision")
        proof = live.get("proof")
        if attempt is not None:
            self.tree.expand([str(attempt)])
        if attempt is not None and revision is not None:
            self.tree.expand([str(attempt), f"{attempt}/{revision}"])
        if attempt is not None and revision is not None and proof is not None:
            self.tree.expand([
                str(attempt), f"{attempt}/{revision}",
                f"{attempt}/{revision}/{proof}"
            ])