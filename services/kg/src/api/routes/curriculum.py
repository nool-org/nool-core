from fastapi import APIRouter

from shared.errors import NotFoundError

from src.api.schemas import ChapterTreeOut, CurriculumSubjectOut, CurriculumTreeOut, SubtopicTreeOut, TopicTreeOut
from src.services.nool_kg_client import get_curriculum_tree, list_curriculum_subjects

router = APIRouter(tags=["curriculum"])


@router.get("/curriculum/subjects", response_model=list[CurriculumSubjectOut])
async def curriculum_subjects() -> list[CurriculumSubjectOut]:
    """Real, populated subject/grade combinations nool-kg currently has -
    read by nool-core's Curriculum page sync picker so a Super Admin
    selects an exact, real KG subject instead of typing a name that has to
    match exactly (a wrong one otherwise silently creates a stray empty
    graph in nool-kg - see nool_kg_client.list_subjects's docstring).
    """
    return [CurriculumSubjectOut(**s) for s in await list_curriculum_subjects()]


@router.get("/curriculum/tree", response_model=CurriculumTreeOut)
async def curriculum_tree(board: str = "CBSE", grade: int = 10, subject: str = "Science") -> CurriculumTreeOut:
    """Read by nool-core's Core service (POST /admin/curriculum/sync-from-kg,
    Super-Admin-triggered) to upsert Subject/Chapter/Topic in Postgres.
    """
    tree = await get_curriculum_tree(board=board, grade=grade, subject=subject)
    if tree is None:
        raise NotFoundError(f'No curriculum in the graph for {board} grade {grade} {subject}.')
    return CurriculumTreeOut(
        board=tree["board"],
        grade=tree["grade"],
        subject=tree["subject"],
        chapters=[
            ChapterTreeOut(
                ref=ch["ref"],
                number=ch["number"],
                name=ch["name"],
                topics=[
                    TopicTreeOut(
                        ref=t["ref"], number=t["number"], name=t["name"],
                        subtopics=[
                            SubtopicTreeOut(ref=s["ref"], number=s["number"], name=s["name"])
                            for s in t.get("subtopics", [])
                        ],
                    )
                    for t in ch["topics"]
                    if t.get("ref") is not None
                ],
            )
            for ch in tree["chapters"]
        ],
    )
