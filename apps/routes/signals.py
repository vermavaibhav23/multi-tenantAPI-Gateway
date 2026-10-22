"""Invalidate Redis routing snapshots after committed configuration changes."""
from django.db import transaction
from django.db.models.signals import post_delete, post_save, pre_delete, pre_save
from django.dispatch import receiver

from .models import BackendService, Route, RouteMethod


def _invalidate_on_commit(project_id, using):
    if project_id is None:
        return
    from gateway.route_cache import invalidate_project_routes
    transaction.on_commit(lambda: invalidate_project_routes(project_id), using=using)


def _old_project_id(sender, instance, using):
    if not instance.pk:
        return None
    return sender.objects.using(using).filter(pk=instance.pk).values_list(
        'project_id', flat=True
    ).first()


@receiver(pre_save, sender=BackendService)
@receiver(pre_save, sender=Route)
def remember_prior_project(sender, instance, using, **kwargs):
    instance._route_cache_old_project_id = _old_project_id(sender, instance, using)


@receiver(pre_save, sender=RouteMethod)
def remember_prior_route(sender, instance, using, **kwargs):
    if not instance.pk:
        return
    previous_route = sender.objects.using(using).filter(pk=instance.pk).values_list(
        'route__project_id', flat=True
    ).first()
    instance._route_cache_old_project_id = previous_route


@receiver(pre_delete, sender=RouteMethod)
def remember_deleted_method_project(sender, instance, using, **kwargs):
    instance._route_cache_old_project_id = instance.route.project_id


@receiver(post_save, sender=BackendService)
@receiver(post_save, sender=Route)
def invalidate_route_or_service(sender, instance, using, **kwargs):
    _invalidate_on_commit(instance.project_id, using)
    old_project_id = getattr(instance, '_route_cache_old_project_id', None)
    if old_project_id != instance.project_id:
        _invalidate_on_commit(old_project_id, using)


@receiver(post_delete, sender=BackendService)
@receiver(post_delete, sender=Route)
def invalidate_deleted_route_or_service(sender, instance, using, **kwargs):
    _invalidate_on_commit(instance.project_id, using)


@receiver(post_save, sender=RouteMethod)
@receiver(post_delete, sender=RouteMethod)
def invalidate_method(sender, instance, using, **kwargs):
    project_id = getattr(instance, '_route_cache_old_project_id', None)
    if project_id is None or instance._state.adding:
        # New policies and ordinary saves: look up their owning project.
        project_id = Route.objects.using(using).filter(
            pk=instance.route_id
        ).values_list('project_id', flat=True).first()
    _invalidate_on_commit(project_id, using)
    other_id = Route.objects.using(using).filter(
        pk=instance.route_id
    ).values_list('project_id', flat=True).first()
    if other_id != project_id:
        _invalidate_on_commit(other_id, using)
