import os
from datetime import timedelta
from typing import List, Dict, Optional

from django.db.models import Count, Avg
from django.utils import timezone

from accounts.models import Student
from content.models import (
    TakeLesson, LessonResource,
    GeneralAssessment, GeneralAssessmentGrade,
    LessonAssessment, LessonAssessmentGrade,
    Question, Option,
    Subject, Topic, Activity,
)
from forum.models import Chat
from .models import AIRecommendation, AIAbuseReport
from elearncore.sysutils.constants import AssessmentType, QType as QTypeEnum, Status as StatusEnum


def _get_openai_client():
    try:
        from openai import OpenAI  # type: ignore
    except Exception:  # pragma: no cover - import guard
        return None
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    return OpenAI(api_key=api_key)


def build_student_activity(student: Student) -> Dict:
    """Summarize a student's activity for prompting the LLM.
    Includes lessons taken, subjects/topics engagement and assessments.
    """
    lessons_qs = (
        TakeLesson.objects
        .filter(student=student)
        .select_related('lesson__subject', 'lesson__topic')
        .order_by('-created_at')[:200]
    )

    general_grades = (
        GeneralAssessmentGrade.objects
        .filter(student=student)
        .select_related('assessment')
        .order_by('-created_at')[:200]
    )

    lesson_grades = (
        LessonAssessmentGrade.objects
        .filter(student=student)
        .select_related('lesson_assessment__lesson')
        .order_by('-created_at')[:200]
    )

    subject_perf = (
        LessonAssessmentGrade.objects
        .filter(student=student)
        .select_related('lesson_assessment__lesson__subject')
        .values('lesson_assessment__lesson__subject__name')
        .annotate(avg_score=Avg('score'), count=Count('id'))
        .order_by('-avg_score')
    )

    data = {
        "student": {
            "id": student.id,
            "name": getattr(student.profile, 'name', ''),
            "grade": student.grade,
        },
        "lessons_taken": [
            {
                "title": tl.lesson.title,
                "subject": tl.lesson.subject.name,
                "topic": tl.lesson.topic.name if tl.lesson.topic else None,
                "type": tl.lesson.type,
                "taken_at": tl.created_at.isoformat(),
            }
            for tl in lessons_qs
        ],
        "general_assessment_grades": [
            {
                "title": g.assessment.title,
                "score": g.score,
                "max": g.assessment.marks,
                "graded_at": g.created_at.isoformat(),
            }
            for g in general_grades
        ],
        "lesson_assessment_grades": [
            {
                "lesson": lg.lesson_assessment.lesson.title,
                "score": lg.score,
                "max": lg.lesson_assessment.marks,
                "graded_at": lg.created_at.isoformat(),
            }
            for lg in lesson_grades
        ],
        "subject_performance": list(subject_perf),
    }
    return data


def _parse_recommendations_json(text: str) -> List[Dict]:
    import json
    try:
        payload = json.loads(text)
        if isinstance(payload, dict) and 'recommendations' in payload:
            return payload['recommendations'] or []
        if isinstance(payload, list):
            return payload
    except Exception:
        pass
    return []


def _match_lesson(subject_name: Optional[str], topic_name: Optional[str], lesson_title: Optional[str] = None) -> Optional[LessonResource]:
    qs = LessonResource.objects.select_related('subject', 'topic').all()
    if subject_name:
        qs = qs.filter(subject__name__iexact=subject_name)
    if topic_name:
        qs = qs.filter(topic__name__iexact=topic_name)
    if lesson_title:
        qs = qs.filter(title__icontains=lesson_title)
    return qs.first()


def generate_recommendations_for_student(student: Student, max_recs: int = 5) -> List[AIRecommendation]:
    """Call the LLM with a student's activity to get course/topic recommendations."""
    client = _get_openai_client()
    if client is None:
        return []

    activity = build_student_activity(student)

    schema = {
        "name": "recommendations_schema",
        "schema": {
            "type": "object",
            "properties": {
                "recommendations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "subject": {"type": "string"},
                            "topic": {"type": ["string", "null"]},
                            "lesson_title": {"type": ["string", "null"]},
                            "reason": {"type": "string"},
                        },
                        "required": ["subject", "reason"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["recommendations"],
            "additionalProperties": False,
        },
        "strict": True,
    }

    prompt = (
        "You are a learning advisor for K-12 in Liberia. Based on the student's recent lessons "
        "and assessment performance, recommend a short prioritized list of subjects/topics/lessons to take next. "
        "Prefer subjects and topics that exist in the provided data context (if any). Keep it at most %d items. "
        "Return strictly as JSON per the provided schema."
    ) % max_recs

    resp = client.responses.create(
        model=os.getenv('OPENAI_RECOMMENDER_MODEL', 'gpt-4o-mini'),
        input=[
            {
                "role": "system",
                "content": "You recommend courses and topics students should study next."
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "input_json", "input_json": activity},
                ],
            },
        ],
        response_format={"type": "json_schema", "json_schema": schema},
    )

    text = resp.output_text
    recs = _parse_recommendations_json(text)
    created: List[AIRecommendation] = []
    for r in recs[:max_recs]:
        subject_name = r.get('subject')
        topic_name = r.get('topic')
        lesson_title = r.get('lesson_title')
        reason = r.get('reason')

        lesson = _match_lesson(subject_name, topic_name, lesson_title)
        if not lesson and subject_name:
            # Try find any lesson by subject name
            subj = Subject.objects.filter(name__iexact=subject_name).first()
            if subj:
                lesson = LessonResource.objects.filter(subject=subj).first()

        if lesson:
            created.append(
                AIRecommendation.objects.create(
                    student=student,
                    lesson=lesson,
                    message=reason or f"Recommended: {subject_name} - {topic_name or ''}".strip(),
                )
            )
    return created


def scan_chats_for_abuse(hours: int = 12) -> List[AIAbuseReport]:
    """Scan recent chats using OpenAI moderation, create abuse reports for flags."""
    client = _get_openai_client()
    if client is None:
        return []

    since = timezone.now() - timedelta(hours=hours)
    chats = Chat.objects.select_related('forum', 'sender').filter(created_at__gte=since).order_by('id')
    reports: List[AIAbuseReport] = []

    for chat in chats:
        content = (chat.content or '').strip()
        if not content:
            continue

        try:
            mod = client.moderations.create(model=os.getenv('OPENAI_MODERATION_MODEL', 'omni-moderation-latest'), input=content)
        except Exception:
            continue

        result = getattr(mod, 'results', [None])[0]
        if not result:
            continue

        flagged = getattr(result, 'flagged', False)
        categories = getattr(result, 'categories', {}) or {}
        if flagged:
            tag = ", ".join([k for k, v in categories.items() if v]) or "FLAGGED"
            desc = f"Flagged categories: {tag}"
            reports.append(
                AIAbuseReport.objects.create(
                    tag=tag[:120],
                    description=desc,
                    mark_review="PENDING",
                    forum=chat.forum,
                    sample_msg=content[:1000],
                )
            )
    return reports


def _parse_assessments_json(text: str) -> List[Dict]:
    """Parse JSON payload for AI-generated assessments.

    Expected top-level structure: {"assessments": [...]} or a bare list.
    """
    import json
    try:
        payload = json.loads(text)
        if isinstance(payload, dict) and 'assessments' in payload:
            return payload['assessments'] or []
        if isinstance(payload, list):
            return payload
    except Exception:
        pass
    return []


def generate_targeted_assessments_for_student(student: Student, max_items: int = 2) -> Dict[str, List[object]]:
    """Use OpenAI to generate targeted quizzes/assignments for a specific student.

    Returns a dictionary with "general" and "lesson" keys containing the
    created GeneralAssessment and LessonAssessment instances. All created
    assessments are flagged as AI recommended and targeted to the student.
    """
    client = _get_openai_client()
    if client is None:
        return {"general": [], "lesson": []}

    activity = build_student_activity(student)

    schema = {
        "name": "targeted_assessments_schema",
        "schema": {
            "type": "object",
            "properties": {
                "assessments": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string", "enum": ["QUIZ", "ASSIGNMENT"]},
                            "scope": {"type": "string", "enum": ["GENERAL", "LESSON"]},
                            "subject": {"type": ["string", "null"]},
                            "topic": {"type": ["string", "null"]},
                            "lesson_title": {"type": ["string", "null"]},
                            "title": {"type": "string"},
                            "instructions": {"type": "string"},
                            "due_in_days": {"type": ["integer", "null"]},
                            "marks": {"type": ["number", "null"]},
                            "questions": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "type": {"type": "string"},
                                        "prompt": {"type": "string"},
                                        "answer": {"type": ["string", "null"]},
                                        "options": {
                                            "type": "array",
                                            "items": {"type": "string"},
                                            "nullable": True,
                                        },
                                    },
                                    "required": ["type", "prompt"],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "required": ["kind", "scope", "title", "instructions"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["assessments"],
            "additionalProperties": False,
        },
        "strict": True,
    }

    prompt = (
        "You are a Liberian K-12 teacher assistant. Based on the student's recent "
        "lessons and assessment performance, propose at most %d new assessments "
        "(quizzes or assignments). Use existing subjects, topics, and lesson titles "
        "from the provided context when possible. Each assessment should be either "
        "GENERAL (not tied to a specific lesson) or LESSON (tied to a concrete lesson). "
        "Return strictly as JSON per the provided schema."
    ) % max_items

    resp = client.responses.create(
        model=os.getenv('OPENAI_RECOMMENDER_MODEL', 'gpt-4o-mini'),
        input=[
            {
                "role": "system",
                "content": (
                    "You generate targeted assessments (quizzes and assignments) "
                    "for a single student on the Liberia eLearn platform. "
                    "Use concise, age-appropriate language."
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "input_json", "input_json": activity},
                ],
            },
        ],
        response_format={"type": "json_schema", "json_schema": schema},
    )

    text = resp.output_text
    items = _parse_assessments_json(text)

    from django.utils import timezone
    from datetime import timedelta

    created_general: List[GeneralAssessment] = []
    created_lesson: List[LessonAssessment] = []

    for item in items[:max_items]:
        kind = (item.get('kind') or '').upper()
        scope = (item.get('scope') or '').upper()
        subject_name = item.get('subject') or None
        topic_name = item.get('topic') or None
        lesson_title = item.get('lesson_title') or None
        title = item.get('title') or "Untitled Assessment"
        instructions = item.get('instructions') or ""
        marks = item.get('marks')
        questions = item.get('questions') or []
        due_in_days = item.get('due_in_days')
        due_at = None
        if isinstance(due_in_days, int) and due_in_days > 0:
            try:
                from django.utils import timezone as _tz
                due_at = _tz.now() + timedelta(days=due_in_days)
            except Exception:
                pass

        assessment_type = AssessmentType.QUIZ.value if kind == 'QUIZ' else AssessmentType.ASSIGNMENT.value
        teacher = getattr(student.profile, 'teacher', None)

        if scope == 'LESSON':
            lesson = _match_lesson(subject_name, topic_name, lesson_title)
            if not lesson:
                continue
            la = LessonAssessment.objects.create(
                lesson=lesson,
                given_by=teacher,
                title=title,
                type=assessment_type,
                instructions=instructions,
                marks=float(marks) if marks is not None else 0.0,
                due_at=due_at,
                status=StatusEnum.PENDING.value,
                ai_recommended=True,
                is_targeted=True,
                target_student=student,
            )
            _created_questions = []
            for q in questions:
                _created_questions.append(
                    _create_question_from_ai(q, lesson_assessment=la, general_assessment=None)
                )
            created_lesson.append(la)
        else:
            ga = GeneralAssessment.objects.create(
                given_by=teacher,
                title=title,
                type=assessment_type,
                instructions=instructions,
                marks=float(marks) if marks is not None else 0.0,
                due_at=due_at,
                grade=student.grade,
                status=StatusEnum.PENDING.value,
                ai_recommended=True,
                is_targeted=True,
                target_student=student,
            )
            for q in questions:
                _create_question_from_ai(q, lesson_assessment=None, general_assessment=ga)
            created_general.append(ga)

        # Log a generic activity for the student so dashboards can surface this
        try:
            user = getattr(student, 'profile', None)
            if user is not None:
                Activity.objects.create(
                    user=user,
                    type="ai_targeted_assessment_created",
                    description=f"AI created assessment '{title}'",
                    metadata={
                        "kind": kind,
                        "scope": scope,
                        "assessment_type": assessment_type,
                    },
                )
        except Exception:
            pass

    return {"general": created_general, "lesson": created_lesson}


def _create_question_from_ai(data: Dict, *, lesson_assessment: Optional[LessonAssessment], general_assessment: Optional[GeneralAssessment]) -> Optional[Question]:
    """Create a Question (and options) from an AI JSON fragment."""
    qtype_raw = (data.get('type') or '').upper()
    valid_types = {qt.value for qt in QTypeEnum}
    if qtype_raw not in valid_types:
        qtype_raw = QTypeEnum.MULTIPLE_CHOICE.value
    question_text = data.get('prompt') or ""
    answer = data.get('answer') or ""
    options_raw = data.get('options') or []

    question = Question.objects.create(
        general_assessment=general_assessment,
        lesson_assessment=lesson_assessment,
        type=qtype_raw,
        question=question_text,
        answer=answer,
    )

    # For TRUE_FALSE, ensure we always have standard options
    if qtype_raw == QTypeEnum.TRUE_FALSE.value:
        options_raw = ["True", "False"]

    for opt in options_raw:
        text = str(opt).strip()
        if text:
            Option.objects.create(question=question, value=text)

    return question

