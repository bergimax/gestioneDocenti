from django.db import models
from django.db.models import Q


class EntryKind(models.TextChoices):
    DELIVERED = "DELIVERED", "Erogata"


class _AppendOnlyQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise NotImplementedError("ledger entries are never updated")

    def delete(self):
        raise NotImplementedError("ledger entries are never deleted")


class LedgerEntry(models.Model):
    """One append-only movement of the hours of an assignment (architecture 4).

    The delivered hours of an assignment are the sum of its entries' `hours`;
    the remaining hours are `Assignment.ore_totali` minus that sum.
    """

    assignment = models.ForeignKey(
        "school.Assignment", on_delete=models.PROTECT, related_name="ledger_entries"
    )
    timetable = models.ForeignKey(
        "scheduling.Timetable", on_delete=models.PROTECT, related_name="ledger_entries"
    )
    week = models.DateField("inizio settimana")
    hours = models.IntegerField()
    kind = models.CharField(max_length=20, choices=EntryKind.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = _AppendOnlyQuerySet.as_manager()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(kind__in=EntryKind.values), name="ledgerentry_kind_valid"
            ),
        ]

    def __str__(self):
        return f"{self.week} {self.kind} {self.hours}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise NotImplementedError("ledger entries are never updated")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise NotImplementedError("ledger entries are never deleted")
