from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import ValidationError
from django.db import transaction

from routes.demo import INDUSTRY_BUNDLES
from routes.destinations import validate_target_url
from routes.models import BackendService, Route
from users.models import Project


class Command(BaseCommand):
    help = 'Create missing industry demo projects and one base route per service; preserve existing configuration.'

    def add_arguments(self, parser):
        parser.add_argument('--admin-email', required=True, help='Existing active staff account that owns the bundles.')

    @transaction.atomic
    def handle(self, *args, **options):
        owner = get_user_model().objects.filter(email=options['admin_email'], is_staff=True, is_active=True).first()
        if owner is None:
            raise CommandError('Create an active staff account first (python manage.py createsuperuser).')
        for slug, (name, services) in INDUSTRY_BUNDLES.items():
            project, _ = Project.objects.get_or_create(slug=slug, defaults={
                'owner': owner, 'name': name,
                'description': 'Shared dummy backend bundle for routing demonstrations only.',
            })
            if project.owner_id != owner.id:
                raise CommandError(f'Project {slug} belongs to another administrator; no data changed.')
            for service_name, host, path in services:
                target = f'http://{host}:8000{path}'
                try:
                    validate_target_url(target)
                except ValidationError as exc:
                    raise CommandError(f'Approve the demo backend origin for {host} before seeding.') from exc
                service, _ = BackendService.objects.get_or_create(
                    project=project, slug=host,
                    defaults={'name': service_name, 'base_path': path, 'target_url': target},
                )
                if service.base_path != path:
                    raise CommandError(f'Existing service {host} uses {service.base_path}, expected {path}.')
                Route.objects.get_or_create(project=project, backend_service=service, defaults={
                    'name': f'{service_name} routing test', 'auth_policy': Route.AUTH_API_KEY_ONLY,
                })
        self.stdout.write(self.style.SUCCESS('Demo bundles ready. Create business applications using /admin-api/applications.'))
