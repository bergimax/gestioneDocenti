import secrets

from django.db import transaction

from scheduling import audit
from school.models import Class, Teacher

from .models import FeedToken

KIND_TEACHER = "teacher"
KIND_CLASS = "class"


def new_token():
    return secrets.token_urlsafe(32)


def ensure_tokens(school):
    """Create a token for every active teacher and class that has none."""
    teachers = Teacher.objects.filter(school=school, active=True, feed_token=None)
    classes = Class.objects.filter(school=school, feed_token=None)
    FeedToken.objects.bulk_create(
        [FeedToken(school=school, teacher=t, token=new_token()) for t in teachers]
        + [FeedToken(school=school, school_class=c, token=new_token()) for c in classes]
    )


def feed_target(school, kind, pk):
    """The FeedToken row for a teacher (active) or class of the school, or None."""
    if kind == KIND_TEACHER:
        return (
            FeedToken.objects.select_for_update()
            .filter(school=school, teacher_id=pk, teacher__active=True)
            .select_related("teacher")
            .first()
        )
    if kind == KIND_CLASS:
        return (
            FeedToken.objects.select_for_update()
            .filter(school=school, school_class_id=pk)
            .select_related("school_class")
            .first()
        )
    return None


def regenerate(school, kind, pk, user):
    """Replace the token and audit it, in one transaction. None if not found."""
    with transaction.atomic():
        feed = feed_target(school, kind, pk)
        if feed is None:
            return None
        feed.token = new_token()
        feed.save(update_fields=["token"])
        name = feed.teacher.name if kind == KIND_TEACHER else feed.school_class.name
        # never the token itself
        audit.record_audit(
            audit.FEED_REGENERATED, user, None, {"tipo": kind, "nome": name}
        )
        return feed
