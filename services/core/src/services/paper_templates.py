"""Blueprint <-> template JSON. The snapshot is the paper's own output
filtered to the create-body fields, so whatever the blueprint grows (C9's
bloomTargets/difficultyMix/...) is carried without touching this file."""

from src.api.schemas.question_paper import CreateQuestionPaperIn, QuestionPaperOut


def snapshot_blueprint(paper: QuestionPaperOut) -> dict:
    fields = CreateQuestionPaperIn.model_fields.keys() - {"name"}
    return {k: v for k, v in paper.model_dump(mode="json").items() if k in fields}


def blueprint_body(blueprint: dict, name: str, chapter_ids: list[str] | None,
                   topic_ids: list[str] | None) -> CreateQuestionPaperIn:
    data = {**blueprint, "name": name}
    if chapter_ids is not None:
        # the template's topics sit under its chapters, so new chapters drop them
        data["chapter_ids"], data["topic_ids"] = chapter_ids, topic_ids
    elif topic_ids is not None:
        data["topic_ids"] = topic_ids
    return CreateQuestionPaperIn.model_validate(data)
