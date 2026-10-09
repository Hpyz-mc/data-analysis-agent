"""Apply explicit corrections without reviving a previously rejected constraint."""

import re


def clarify_followup(previous: str, answer: str, question: str) -> str:
    combined = f"{previous}；用户补充：{question}"
    if "数据表" in answer and ("不一致" in answer or "不在可访问" in answer):
        names = re.findall(r"\bpublic\.([^\W\d]\w*)", question)
        if not names:
            match = re.fullmatch(
                r"(?:请)?(?:使用|改用|选择|换成|用)\s*(?:数据表\s*)?([^\W\d]\w*)[。！]?",
                question.strip(),
            )
            names = [match[1]] if match else []
        if len(names) == 1:
            # Preserve the original metric/period, but replace the rejected table.
            corrected = re.sub(r"\bpublic\.[^\W\d]\w*", "public." + names[0], previous)
            # Only rewrite a bare identifier in an explicit table position;
            # English metric names elsewhere must remain unchanged.
            corrected = re.sub(
                r"((?:数据表|使用表)\s*|查询\s+)([A-Za-z_]\w*)(?=\s+的|[。；]|$)",
                lambda match: match[1] + "public." + names[0], corrected,
            )
            return f"{corrected}；用户更正：使用 public.{names[0]}。{question}"
    missing = re.search(r"当前表没有字段 ([\w]+)，", answer)
    replacement = re.fullmatch(
        r"(?:那就|那|请)?(?:改为|改用|换成|使用|用)\s*[`\"“]?([^\W\d]\w*?)[`\"”]?(?:字段)?[。！]?",
        question.strip(),
    )
    if missing and replacement:
        # A conditional fallback ('if absent, do not substitute ...') belongs to
        # the rejected request, not the user's newly chosen metric.
        clauses = re.split(r"([。！？\n])", previous)
        corrected = "".join(
            clause for clause in clauses
            if not re.search(r"(?:如果|若).*(?:没有|缺少|不存在).*字段|不要用.*代替", clause)
        ).replace(missing[1], replacement[1])
        return f"{corrected}；用户更正：{question}"
    return combined
