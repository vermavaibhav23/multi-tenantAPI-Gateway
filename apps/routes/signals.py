"""Invalidate per-route Redis entries after committed route/service changes."""
from django.db import transaction
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from .models import BackendService, Route


def _invalidate_after_commit(project_id, base_path, using):
    if project_id is None or not base_path:
        return
    from gateway.route_cache import invalidate_route

    transaction.on_commit(
        lambda project_id=project_id, base_path=base_path: invalidate_route(project_id, base_path),
        using=using,
    )


@receiver(pre_save, sender=Route)
def remember_previous_route(sender, instance, using, **kwargs):
    instance._route_cache_previous = None
    if instance.pk:
        instance._route_cache_previous = sender.objects.using(using).filter(
            pk=instance.pk
        ).values_list('project_id', 'base_path').first()


@receiver(post_save, sender=Route)
def invalidate_saved_route(sender, instance, using, **kwargs):
    _invalidate_after_commit(instance.project_id, instance.base_path, using)
    previous = getattr(instance, '_route_cache_previous', None)
    if previous is not None and previous != (instance.project_id, instance.base_path):
        _invalidate_after_commit(previous[0], previous[1], using)


@receiver(post_delete, sender=Route)
def invalidate_deleted_route(sender, instance, using, **kwargs):
    _invalidate_after_commit(instance.project_id, instance.base_path, using)


@receiver(post_save, sender=BackendService)
def invalidate_routes_for_changed_service(sender, instance, using, **kwargs):
    # A shared backend can own several distinct base paths.
    paths = Route.objects.using(using).filter(backend_service_id=instance.pk).values_list(
        'project_id', 'base_path'
    )
    for project_id, base_path in paths:
        _invalidate_after_commit(project_id, base_path, using)
