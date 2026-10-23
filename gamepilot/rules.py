"""Actions defined by the supplied P1-P6 plan, independent of feature confidence."""

PAGE_LABELS = {
    "P1": "确认", "P2": "进入下一天", "P3": "跳过",
    "P4": "进入新阶段", "P5": "关闭", "P6": "重新开始",
}
NORMAL_LABELS = {state: label for state, label in PAGE_LABELS.items() if state != "P6"}

# Stage 2 never fights the layout: on the garden board the top-right X leaves the
# park, which opens the pause menu where "重新开始" returns to stage 1.
# Reference-space box measured from the live client at 1705x1066.
STAGE2_EXIT_BOX = (1565, 15, 1645, 90)
STAGE2_EXIT_LABEL = "退出乐园"
STAGE2_PAGES = ("P1", "P2", "P5")


def validate_manifest(manifest, reference_size):
    if not isinstance(manifest, dict) or set(manifest) != set(PAGE_LABELS):
        raise ValueError("模板清单必须且只能包含 P1～P6")
    width, height = reference_size
    for state, label in PAGE_LABELS.items():
        spec = manifest[state]
        if not isinstance(spec, dict) or spec.get("label") != label:
            raise ValueError(f"{state} 只允许动作：{label}")
        for kind in ("anchor", "button"):
            box = spec.get(kind)
            if (not isinstance(box, list) or len(box) != 4
                    or not all(type(value) is int for value in box)
                    or not (0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height)):
                raise ValueError(f"{state} 的 {kind} 模板范围无效")
