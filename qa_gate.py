RUBRIC = {
    "version": "2026-10-a",
    "criteria": {
        "facts": "Every student name, roll number, course, and mark matches the database records.",
        "policy": "No unauthorized grade changes or unverified student claims.",
        "fit": "Directly answers the admin's request in a clear, structured format.",
        "clarity": "Explains the exact database change proposed for human review.",
    },
}


def check_review(review: dict, rubric: dict = RUBRIC) -> list[str]:
    if not isinstance(review, dict):
        return ["review must be a dict"]
    problems = []
    scores = review.get("scores")
    if not isinstance(scores, dict):
        problems.append("scores must be a dict")
    else:
        for criterion in rubric["criteria"]:
            val = scores.get(criterion)
            if isinstance(val, bool) or not isinstance(val, int) or not (1 <= val <= 10):
                problems.append(f"{criterion}: score must be int 1..10")
    if not isinstance(review.get("notes"), str):
        problems.append("notes must be a str")
    return problems


def run_qa_gate(task: str, writer, reviewer, sources: list[str], threshold: int = 8) -> dict:
    """Blind independent QA gate: reviewer sees only (draft, rubric, sources)."""
    if reviewer is writer:
        raise ValueError("Reviewer must be independent from writer")

    feedback = None
    review_log = []
    draft = ""

    for attempt in (1, 2):
        draft = writer(task, feedback=feedback)
        review = reviewer(draft, RUBRIC, sources)
        problems = check_review(review, RUBRIC)

        if problems:
            notes = "; ".join(problems)
            review_log.append({"attempt": attempt, "scores": None, "passed": False, "notes": notes})
            feedback = notes
            continue

        scores = review["scores"]
        notes = review["notes"]
        failing = [k for k in RUBRIC["criteria"] if scores[k] < threshold]
        passed = len(failing) == 0
        review_log.append({"attempt": attempt, "scores": scores, "passed": passed, "notes": notes})

        if passed:
            return {"status": "approved", "draft": draft, "attempts": attempt, "scores": scores, "log": review_log}

        feedback = f"Failed criteria ({', '.join(failing)}): {notes}"

    return {"status": "held", "draft": draft, "attempts": 2, "reason": feedback, "log": review_log}
