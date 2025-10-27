import os
from datetime import timedelta
from typing import List, Dict, Optional

from django.db.models import Count, Avg
from django.utils import timezone

from accounts.models import Student
from content.models import (
    TakeLesson, LessonResource,
    GeneralAssessmentGrade, LessonAssessmentGrade,
    Subject, Topic,
)
from forum.models import Chat
from .models import AIRecommendation, AIAbuseReport


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


def _match_lesson(subject_name: Optional[str], topic_name: Optional[str]) -> Optional[LessonResource]:
    qs = LessonResource.objects.select_related('subject', 'topic').all()
    if subject_name:
        qs = qs.filter(subject__name__iexact=subject_name)
    if topic_name:
        qs = qs.filter(topic__name__iexact=topic_name)
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
        reason = r.get('reason')

        lesson = _match_lesson(subject_name, topic_name)
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
