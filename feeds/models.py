from django.db import models
from django.db.models import Q

from school.models import Class, School, Teacher


class FeedToken(models.Model):
    """The secret URL part of one iCalendar feed: a teacher's or a class's.

    The token is stored in clear text on purpose (unlike AvailabilityLink,
    which is hashed and shown once): the ADMIN must be able to see every feed
    URL again. Anyone with database access can therefore read the URLs; they
    expose only the approved timetable. Regenerating replaces the token in
    place, so nothing accumulates.
    """

    school = models.ForeignKey(
        School, on_delete=models.CASCADE, related_name="feed_tokens"
    )
    teacher = models.OneToOneField(
        Teacher,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="feed_token",
    )
    school_class = models.OneToOneField(
        Class,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="feed_token",
    )
    token = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(Q(teacher__isnull=False) & Q(school_class__isnull=True))
                | (Q(teacher__isnull=True) & Q(school_class__isnull=False)),
                name="feedtoken_teacher_xor_class",
            ),
        ]

    def __str__(self):
        return f"feed {self.teacher_id or self.school_class_id}"


class FeedLookupFailure(models.Model):
    """One failed feed lookup, kept in Postgres for the rate limit.

    Holds only the client address and the time: never the token.
    """

    client_address = models.CharField(max_length=64, db_index=True)
    attempted_at = models.DateTimeField(db_index=True)

    def __str__(self):
        return f"{self.client_address} {self.attempted_at}"
