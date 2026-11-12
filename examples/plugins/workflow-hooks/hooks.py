def handle(event, context):
    data = context["data"]
    if event == "before_chat":
        return {"context": "When changing code, finish by reporting which checks actually ran."}
    if event == "before_tool" and data["tool"] == "run_command":
        if "git push --force" in data["args"].get("command", ""):
            return {"block": "This workflow does not allow force pushes."}
    return {}
