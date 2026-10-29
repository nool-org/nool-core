"""C-005 (2023 rationalised NCERT): drop the bank's periodic-classification and Euclid's
division-lemma questions, move Metals questions tagged to Chemical Reactions to Metals and
Non-metals, and fix the answer key that said "Na and K are in the same group" when Na
isn't listed. Matched by external_uid (the same in every environment) plus the text, so
it touches only these rows. Idempotent.

Revision ID: f4d6e8a0b2c3
Revises: e3c5d7f9a1b2
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'f4d6e8a0b2c3'
down_revision: Union[str, Sequence[str], None] = 'e3c5d7f9a1b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# read one by one: periodic classification (Mendeleev, Modern Periodic Table, groups and
# periods) and Euclid's division lemma - both out of the 2023 syllabus. A question that only
# names an element's group as a clue (plaster of Paris, carbon's compounds, sulphur) stays.
OUT_OF_SYLLABUS = """
    'CH01:pdf-cdefdb5b-ef40-4154-92fe-410890261ca0-3 OR',
    'CH01:pdf-cdefdb5b-ef40-4154-92fe-410890261ca0-3',
    'CH03:pdf-0b971cb0-9595-4940-99f6-efcc199a8dd6-12',
    'CH03:pdf-0dedcf24-c282-41f3-baa3-37ae3527b9b4-8',
    'CH03:pdf-13828-17',
    'CH03:pdf-15148-5',
    'CH03:pdf-2700-10',
    'CH03:pdf-2700-9',
    'CH03:pdf-2779-10',
    'CH03:pdf-2779-9',
    'CH03:pdf-3594-Q10',
    'CH03:pdf-3594-Q9',
    'CH03:pdf-3595-Q10',
    'CH03:pdf-3596-Q9',
    'CH03:pdf-3597-Q7',
    'CH03:pdf-3597-Q9',
    'CH03:pdf-3598-Q9',
    'CH03:pdf-4482-50',
    'CH03:pdf-4772933f-01ce-495c-abc4-06d164b26e3b-10',
    'CH03:pdf-4772933f-01ce-495c-abc4-06d164b26e3b-3',
    'CH03:pdf-4ced4054-8fb1-4647-b67f-deb15d2b13cc-7',
    'CH03:pdf-4d81b2fa-8d6f-4ec2-915a-41ce80036b8b-3',
    'CH03:pdf-502155ef-00e9-4730-aa6b-802fcda44232-1',
    'CH03:pdf-54e95b34-510f-4912-a5ac-aab461d2374a-1',
    'CH03:pdf-6239-5',
    'CH03:pdf-6239-6',
    'CH03:pdf-6315-15',
    'CH03:pdf-6315-5',
    'CH03:pdf-6963-13',
    'CH03:pdf-6cf2a8a6-5167-4805-8a1f-268dca957368-2',
    'CH03:pdf-85dd6762-7c8a-4260-bd8d-595b35f1a4ec-2',
    'CH03:pdf-85dd6762-7c8a-4260-bd8d-595b35f1a4ec-8',
    'CH03:pdf-87db4d39-7980-4dea-be6e-15a3a5a1b8e6-3 OR',
    'CH03:pdf-89e085d4-400b-48e8-ae27-ecc3a7a96b96-17',
    'CH03:pdf-89e085d4-400b-48e8-ae27-ecc3a7a96b96-2',
    'CH03:pdf-980a0d07-2b3e-4596-a8e0-f1e7a14b06ac-3',
    'CH03:pdf-9c0f0e89-53e1-416d-96c2-62ea7bb71997-12 (OR)',
    'CH03:pdf-9c0f0e89-53e1-416d-96c2-62ea7bb71997-17',
    'CH03:pdf-9c0f0e89-53e1-416d-96c2-62ea7bb71997-2',
    'CH03:pdf-b383254c-8df1-4aab-9435-0d249a1d62a5-16 (OR)',
    'CH03:pdf-b383254c-8df1-4aab-9435-0d249a1d62a5-16',
    'CH03:pdf-ba6c0f94-ef60-443c-b9b2-a00d2fd461a0-15',
    'CH03:pdf-c25c0205-5fc0-436a-9430-8559ddff22ee-1',
    'CH03:pdf-d4bb2817-27e2-4ecb-9f35-d27732c826a2-6',
    'CH03:pdf-d88b541e-3129-499b-a49e-d13ae54f1911-8',
    'CH03:pdf-d96d9e4c-3787-46af-a9d3-3d9e82b62dcd-17',
    'CH03:pdf-f27f55ea-5e5e-4dc1-b3ba-a3ca8a0fce7b-1',
    'CH03:pdf-f27f55ea-5e5e-4dc1-b3ba-a3ca8a0fce7b-8',
    'CH03:pdf-f72deffc-0b55-4057-8b71-2150b94fe798-6',
    'CH04:pdf-3a709f3b-6902-473e-96f5-e0e9c4e91adc-19',
    'CH04:pyq-1314564',
    'CH08:pdf-4ced4054-8fb1-4647-b67f-deb15d2b13cc-12',
    'CH11:otest-12903',
    'CH13:pdf-1f17452b-ac13-4c50-bc76-090dabfb82ff-29 (OR)',
    'nool-mathe-ch01-142',
    'nool-mathe-ch01-149'
"""
# Metals content tagged "1.2 Types of chemical reactions": amphoteric oxides, calcium in water
# and nitric acid go to 3.2 (chemical properties of metals); ores, roasting, calcination,
# thermit and refining to 3.4 (occurrence of metals). The thermit *equation* rows stay.
TO_3_2 = "'CH01:pyq-1302333', 'CH01:pyq-1302612'"
TO_3_4 = ("'CH01:pdf-1314-20', 'CH01:prac-1750519', 'CH01:pyq-1033341', 'CH01:pyq-1174279', "
          "'CH01:pdf-0b971cb0-9595-4940-99f6-efcc199a8dd6-25', 'CH01:pyq-736891', 'CH01:pyq-1302476', "
          "'CH01:pyq-1163143'")
WRONG_KEY = "Na and K are in the same group."
RIGHT_KEY = "None of them share a group: Be is in group 2, F in group 17, K in group 1 and Cu in group 11."


def upgrade() -> None:
    op.execute(f"""
        DELETE FROM dataset_questions WHERE external_uid IN ({OUT_OF_SYLLABUS})
          AND text ~* '(periodic|mendele|newland|bereiner|octave|triad|euclid|group|period)'
    """)
    for uids, prefix in ((TO_3_2, '3.2 %'), (TO_3_4, '3.4 %')):
        op.execute(f"""
            UPDATE dataset_questions d SET chapter_id = m.id, topic_id = t.id, subtopic_id = NULL
            FROM chapters c, chapters m, topics t
            WHERE d.external_uid IN ({uids}) AND d.chapter_id = c.id
              AND c.name = 'Chemical Reactions and Equations' AND m.subject_id = c.subject_id
              AND m.name = 'Metals and Non-metals' AND m.grade IS NOT DISTINCT FROM c.grade
              AND t.chapter_id = m.id AND t.name LIKE '{prefix}'
        """)
    for table in ("dataset_questions", "homework_questions"):
        op.execute(f"""
            UPDATE {table} SET answer = replace(answer, '{WRONG_KEY}', '{RIGHT_KEY}')
            WHERE answer LIKE '%{WRONG_KEY}%' AND text ~ 'Be.*F.*K.*Cu'
        """)


def downgrade() -> None:
    pass  # out-of-syllabus questions and a wrong key are not restored
