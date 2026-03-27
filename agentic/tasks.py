from celery import shared_task
from django.contrib.auth import get_user_model

from accounts.models import School
from content.models import Story

from .services import generate_story_payload


@shared_task(bind=True)
def generate_stories_task(self, *, requested_by_id: int, grade: str, tag: str, count: int, school_id: int | None = None) -> dict:
    """Generate one or more stories asynchronously and persist them."""
    User = get_user_model()
    requested_by = User.objects.filter(id=requested_by_id).first()
    school = School.objects.filter(id=school_id).first() if school_id else None

    safe_count = max(1, min(int(count), 10))
    created_ids: list[int] = []

    for _ in range(safe_count):
        payload = generate_story_payload(tag=tag, grade=grade)
        story = Story.objects.create(
            title=(payload.get("title") or f"{tag} Story").strip()[:200],
            grade=grade,
            tag=tag,
            estimated_minutes=max(1, int(payload.get("estimated_minutes") or 5)),
            body=(payload.get("body") or "").strip(),
            characters=payload.get("characters") or [],
            vocabulary=payload.get("vocabulary") or [],
            moral=(payload.get("moral") or "").strip()[:255],
            cover_image=payload.get("cover_image") or {},
            is_published=False,
            school=school,
            created_by=requested_by,
        )
        created_ids.append(story.id)

    return {
        "created": len(created_ids),
        "story_ids": created_ids,
        "school_id": school.id if school else None,
        "grade": grade,
        "tag": tag,
    }
