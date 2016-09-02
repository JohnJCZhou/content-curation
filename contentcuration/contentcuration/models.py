import logging
import os
import uuid
import hashlib
import functools

from django.conf import settings
from django.contrib import admin
from django.core.files.storage import FileSystemStorage
from django.db import IntegrityError, connections, models
from django.db.models import Q
from django.db.utils import ConnectionDoesNotExist
from mptt.models import MPTTModel, TreeForeignKey, TreeManager
from django.utils.translation import ugettext as _
from django.dispatch import receiver
from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.core.mail import send_mail, EmailMultiAlternatives
from django.template.loader import render_to_string

from constants import content_kinds, extensions, presets

class UserManager(BaseUserManager):
    def create_user(self, email, first_name, last_name, password=None):
        if not email:
            raise ValueError('Email address not specified')

        new_user = self.model(
            email=self.normalize_email(email),
        )

        new_user.set_password(password)
        new_user.first_name = first_name
        new_user.last_name = last_name
        new_user.save(using=self._db)
        return new_user

    def create_superuser(self, email, first_name, last_name, password=None):
        new_user = self.create_user(email, first_name, last_name, password=password)
        new_user.is_admin = True
        new_user.save(using=self._db)
        return new_user

class User(AbstractBaseUser):
    email = models.EmailField(max_length=100, unique=True)
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    is_admin = models.BooleanField(default=False)
    is_active = models.BooleanField(default=False)
    clipboard_tree =  models.ForeignKey('ContentNode', null=True, blank=True, related_name='user_clipboard')

    objects = UserManager()
    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['first_name', 'last_name']

    def __unicode__(self):
        return self.email

    def email_user(self, subject, message, from_email=None, **kwargs):
        # msg = EmailMultiAlternatives(subject, message, from_email, [self.email])
        # msg.attach_alternative(kwargs["html_message"],"text/html")
        # msg.send()
        send_mail(subject, message, from_email, [self.email], **kwargs)

    def clean(self):
        super(User, self).clean()
        self.email = self.__class__.objects.normalize_email(self.email)

    def get_full_name(self):
        """
        Returns the first_name plus the last_name, with a space in between.
        """
        full_name = '%s %s' % (self.first_name, self.last_name)
        return full_name.strip()

    def get_short_name(self):
        "Returns the short name for the user."
        return self.first_name

    def save(self, *args, **kwargs):
        super(User, self).save(*args, **kwargs)
        if not self.clipboard_tree:
            self.clipboard_tree = ContentNode.objects.create(title=self.email + " clipboard", kind_id="topic", sort_order=0)
            self.clipboard_tree.save()
            self.save()

    class Meta:
        verbose_name = _("User")
        verbose_name_plural = _("Users")

class UUIDField(models.CharField):

    def __init__(self, *args, **kwargs):
        kwargs['max_length'] = 32
        super(UUIDField, self).__init__(*args, **kwargs)

    def get_default(self):
        result = super(UUIDField, self).get_default()
        if isinstance(result, uuid.UUID):
            result = result.hex
        return result

def file_on_disk_name(instance, filename):
    """
    Create a name spaced file path from the File obejct's checksum property.
    This path will be used to store the content copy

    :param instance: File (content File model)
    :param filename: str
    :return: str
    """
    h = instance.checksum
    basename, ext = os.path.splitext(filename)
    return os.path.join(settings.STORAGE_URL[1:-1], h[0], h[1], h + ext.lower())

class FileOnDiskStorage(FileSystemStorage):
    """
    Overrider FileSystemStorage's default save method to ignore duplicated file.
    """
    def get_available_name(self, name):
        return name

    def _save(self, name, content):
        if self.exists(name):
            # if the file exists, do not call the superclasses _save method
            logging.warn('Content copy "%s" already exists!' % name)
            return name
        return super(FileOnDiskStorage, self)._save(name, content)

class Channel(models.Model):
    """ Permissions come from association with organizations """
    id = UUIDField(primary_key=True, default=uuid.uuid4)
    name = models.CharField(max_length=200)
    description = models.CharField(max_length=400, blank=True)
    version = models.IntegerField(default=0)
    thumbnail = models.TextField(blank=True)
    editors = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name='editable_channels',
        verbose_name=_("editors"),
        help_text=_("Users with edit rights"),
    )
    trash_tree =  models.ForeignKey('ContentNode', null=True, blank=True, related_name='channel_trash')
    clipboard_tree =  models.ForeignKey('ContentNode', null=True, blank=True, related_name='channel_clipboard')
    main_tree =  models.ForeignKey('ContentNode', null=True, blank=True, related_name='channel_main')
    bookmarked_by = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name='bookmarked_channels',
        verbose_name=_("bookmarked by"),
    )
    deleted = models.BooleanField(default=False)
    public = models.BooleanField(default=False)

    def save(self, *args, **kwargs):
        super(Channel, self).save(*args, **kwargs)
        if not self.main_tree:
            self.main_tree = ContentNode.objects.create(title=self.name + " main root", kind_id="topic", sort_order=0)
            self.main_tree.save()
            self.save()
        if not self.trash_tree:
            self.trash_tree = ContentNode.objects.create(title=self.name + " trash root", kind_id="topic", sort_order=0)
            self.trash_tree.save()
            self.save()
    class Meta:
        verbose_name = _("Channel")
        verbose_name_plural = _("Channels")

class ContentTag(models.Model):
    id = UUIDField(primary_key=True, default=uuid.uuid4)
    tag_name = models.CharField(max_length=30)
    channel = models.ForeignKey('Channel', related_name='tags', blank=True, null=True)

    def __str__(self):
        return self.tag_name

    class Meta:
        unique_together = ['tag_name', 'channel']

def delegate_manager(method):
    """
    Delegate method calls to base manager, if exists.
    """
    @functools.wraps(method)
    def wrapped(self, *args, **kwargs):
        if self._base_manager:
            return getattr(self._base_manager, method.__name__)(*args, **kwargs)
        return method(self, *args, **kwargs)
    return wrapped

class ContentNode(MPTTModel, models.Model):
    """
    By default, all nodes have a title and can be used as a topic.
    """
    # The id should be the same between the content curation server and Kolibri.
    id = UUIDField(primary_key=True, default=uuid.uuid4)

    # the content_id is used for tracking a user's interaction with a piece of
    # content, in the face of possibly many copies of that content. When a user
    # interacts with a piece of content, all substantially similar pieces of
    # content should be marked as such as well. We track these "substantially
    # similar" types of content by having them have the same content_id.
    content_id = UUIDField(primary_key=False, default=uuid.uuid4, editable=False)

    title = models.CharField(max_length=200)
    description = models.CharField(max_length=400, blank=True)
    kind = models.ForeignKey('ContentKind', related_name='contentnodes')
    license = models.ForeignKey('License', null=True)
    prerequisite = models.ManyToManyField('self', related_name='is_prerequisite_of', through='PrerequisiteContentRelationship', symmetrical=False, blank=True)
    is_related = models.ManyToManyField('self', related_name='relate_to', through='RelatedContentRelationship', symmetrical=False, blank=True)
    parent = TreeForeignKey('self', null=True, blank=True, related_name='children', db_index=True)
    tags = models.ManyToManyField(ContentTag, symmetrical=False, related_name='tagged_content', blank=True)
    sort_order = models.FloatField(max_length=50, default=1, verbose_name=_("sort order"), help_text=_("Ascending, lowest number shown first"))
    copyright_holder = models.CharField(max_length=200, blank=True, help_text=_("Organization of person who holds the essential rights"))
    author = models.CharField(max_length=200, blank=True, help_text=_("Person who created content"))
    cloned_source = TreeForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='clones')
    original_node = TreeForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='duplicates')

    created = models.DateTimeField(auto_now_add=True, verbose_name=_("created"))
    modified = models.DateTimeField(auto_now=True, verbose_name=_("modified"))
    published = models.BooleanField(default=False)

    changed = models.BooleanField(default=True)

    objects = TreeManager()

    def __init__(self, *args, **kwargs):
        super(ContentNode, self).__init__(*args, **kwargs)
        self.original_parent = self.parent

    def save(self, *args, **kwargs):
        isNew = self.pk is None

        # Detect if model has been moved to a different tree
        if self.original_parent and self.original_parent.id != self.parent_id:
            self.original_parent.changed = True
            self.original_parent.save()
            self.original_parent = self.parent

        super(ContentNode, self).save(*args, **kwargs)
        if isNew:
            if self.original_node is None:
                self.original_node = self.pk
            if self.cloned_source is None:
                self.cloned_source = self.pk
            self.save()

    class MPTTMeta:
        order_insertion_by = ['sort_order']

    class Meta:
        verbose_name = _("Topic")
        verbose_name_plural = _("Topics")
        # Do not allow two nodes with the same name on the same level
        #unique_together = ('parent', 'title')


class ContentKind(models.Model):
    kind = models.CharField(primary_key=True, max_length=200, choices=content_kinds.choices)

    def __str__(self):
        return self.kind

class FileFormat(models.Model):
    extension = models.CharField(primary_key=True, max_length=40, choices=extensions.choices)
    mimetype = models.CharField(max_length=200, blank=True)

    def __str__(self):
        return self.extension

class FormatPreset(models.Model):
    id = models.CharField(primary_key=True, max_length=150, choices=presets.choices)
    readable_name = models.CharField(max_length=400)
    multi_language = models.BooleanField(default=False)
    supplementary = models.BooleanField(default=False)
    thumbnail = models.BooleanField(default=False)
    order = models.IntegerField()
    kind = models.ForeignKey(ContentKind, related_name='format_presets')
    allowed_formats = models.ManyToManyField(FileFormat, blank=True)

    def __str__(self):
        return self.id

class Language(models.Model):
    lang_code = models.CharField(max_length=2, db_index=True)
    lang_subcode = models.CharField(max_length=2, db_index=True)
    readable_name = models.CharField(max_length=50, blank=True)

    def ietf_name(self):
        return "{code}-{subcode}".format(code=self.lang_code, subcode=self.lang_subcode)

    def __str__(self):
        return self.ietf_name

class File(models.Model):
    """
    The bottom layer of the contentDB schema, defines the basic building brick for content.
    Things it can represent are, for example, mp4, avi, mov, html, css, jpeg, pdf, mp3...
    """
    id = UUIDField(primary_key=True, default=uuid.uuid4)
    checksum = models.CharField(max_length=400, blank=True)
    file_size = models.IntegerField(blank=True, null=True)
    file_on_disk = models.FileField(upload_to=file_on_disk_name, storage=FileOnDiskStorage(), max_length=500, blank=True)
    contentnode = models.ForeignKey(ContentNode, related_name='files', blank=True, null=True)
    file_format = models.ForeignKey(FileFormat, related_name='files', blank=True, null=True)
    preset = models.ForeignKey(FormatPreset, related_name='files', blank=True, null=True)
    lang = models.ForeignKey(Language, blank=True, null=True)
    original_filename = models.CharField(max_length=255, blank=True)
    source_url = models.CharField(max_length=400, blank=True)

    class Admin:
        pass

    def __str__(self):
        return '{checksum}{extension}'.format(checksum=self.checksum, extension='.' + self.file_format.extension)

    def save(self, *args, **kwargs):
        """
        Overrider the default save method.
        If the file_on_disk FileField gets passed a content copy:
            1. generate the MD5 from the content copy
            2. fill the other fields accordingly
        """
        if self.file_on_disk:  # if file_on_disk is supplied, hash out the file
            md5 = hashlib.md5()
            for chunk in self.file_on_disk.chunks():
                md5.update(chunk)

            self.checksum = md5.hexdigest()
            self.file_size = self.file_on_disk.size
            self.extension = os.path.splitext(self.file_on_disk.name)[1]
        else:
            self.checksum = None
            self.file_size = None
            self.extension = None
        super(File, self).save(*args, **kwargs)

@receiver(models.signals.post_delete, sender=File)
def auto_delete_file_on_delete(sender, instance, **kwargs):
    """
    Deletes file from filesystem if no other File objects are referencing the same file on disk
    when corresponding `File` object is deleted.
    Be careful! we don't know if this will work when perform bash delete on File obejcts.
    """
    if not File.objects.filter(file_on_disk=instance.file_on_disk.url):
        file_on_disk_path = os.path.join(settings.STORAGE_ROOT, instance.checksum[0:1], instance.checksum[1:2], instance.checksum + '.' + instance.file_format.extension)
        print file_on_disk_path
        if os.path.isfile(file_on_disk_path):
            os.remove(file_on_disk_path)

class License(models.Model):
    """
    Normalize the license of ContentNode model
    """
    license_name = models.CharField(max_length=50)
    license_url = models.URLField(blank=True)
    license_description = models.TextField(blank=True)
    exists = models.BooleanField(
        default=False,
        verbose_name=_("license exists"),
        help_text=_("Tells whether or not a content item is licensed to share"),
    )

    def __str__(self):
        return self.license_name

class PrerequisiteContentRelationship(models.Model):
    """
    Predefine the prerequisite relationship between two ContentNode objects.
    """
    target_node = models.ForeignKey(ContentNode, related_name='%(app_label)s_%(class)s_target_node')
    prerequisite = models.ForeignKey(ContentNode, related_name='%(app_label)s_%(class)s_prerequisite')

    class Meta:
        unique_together = ['target_node', 'prerequisite']

    def clean(self, *args, **kwargs):
        # self reference exception
        if self.target_node == self.prerequisite:
            raise IntegrityError('Cannot self reference as prerequisite.')
        # immediate cyclic exception
        elif PrerequisiteContentRelationship.objects.using(self._state.db)\
                .filter(target_node=self.prerequisite, prerequisite=self.target_node):
            raise IntegrityError(
                'Note: Prerequisite relationship is directional! %s and %s cannot be prerequisite of each other!'
                % (self.target_node, self.prerequisite))
        # distant cyclic exception
        # elif <this is a nice to have exception, may implement in the future when the priority raises.>
        #     raise Exception('Note: Prerequisite relationship is acyclic! %s and %s forms a closed loop!' % (self.target_node, self.prerequisite))
        super(PrerequisiteContentRelationship, self).clean(*args, **kwargs)

    def save(self, *args, **kwargs):
        self.full_clean()
        super(PrerequisiteContentRelationship, self).save(*args, **kwargs)



class RelatedContentRelationship(models.Model):
    """
    Predefine the related relationship between two ContentNode objects.
    """
    contentnode_1 = models.ForeignKey(ContentNode, related_name='%(app_label)s_%(class)s_1')
    contentnode_2 = models.ForeignKey(ContentNode, related_name='%(app_label)s_%(class)s_2')

    class Meta:
        unique_together = ['contentnode_1', 'contentnode_2']

    def save(self, *args, **kwargs):
        # self reference exception
        if self.contentnode_1 == self.contentnode_2:
            raise IntegrityError('Cannot self reference as related.')
        # handle immediate cyclic
        elif RelatedContentRelationship.objects.using(self._state.db)\
                .filter(contentnode_1=self.contentnode_2, contentnode_2=self.contentnode_1):
            return  # silently cancel the save
        super(RelatedContentRelationship, self).save(*args, **kwargs)

class Exercise(models.Model):

    title = models.CharField(
        max_length=50,
        verbose_name=_("title"),
        default=_("Title"),
        help_text=_("Title of the content item"),
    )

    description = models.TextField(
        max_length=200,
        verbose_name=_("description"),
        default=_("Description"),
        help_text=_("Brief description of what this content item is"),
    )

class AssessmentItem(models.Model):

    type = models.CharField(max_length=50, default="multiplechoice")
    question = models.TextField(blank=True)
    answers = models.TextField(default="[]")
    exercise = models.ForeignKey('Exercise', related_name="all_assessment_items")

class Invitation(models.Model):
    """ Invitation to edit channel """
    id = UUIDField(primary_key=True, default=uuid.uuid4)
    invited = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, related_name='sent_to')
    email = models.EmailField(max_length=100)
    sender = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='sent_by')
    channel = models.ForeignKey('Channel', null=True, related_name='pending_editors')
    first_name = models.CharField(max_length=100, default='Guest')
    last_name = models.CharField(max_length=100, blank=True, null=True)

    class Meta:
        verbose_name = _("Invitation")
        verbose_name_plural = _("Invitations")
