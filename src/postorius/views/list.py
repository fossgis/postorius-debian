# -*- coding: utf-8 -*-
# Copyright (C) 1998-2019 by the Free Software Foundation, Inc.
#
# This file is part of Postorius.
#
# Postorius is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version.
#
# Postorius is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License for
# more details.
#
# You should have received a copy of the GNU General Public License along with
# Postorius.  If not, see <http://www.gnu.org/licenses/>.


import csv
import email.utils
import logging

from allauth.account.models import EmailAddress
from django.http import HttpResponse, HttpResponseNotAllowed, Http404
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.validators import validate_email
from django.forms import formset_factory
from django.shortcuts import render, redirect
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.translation import gettext as _
from django_mailman3.lib.mailman import get_mailman_client
from django_mailman3.lib.paginator import paginate, MailmanPaginator
from django.utils.six.moves.urllib.error import HTTPError

from postorius.forms import (
    ListNew, MemberForm, ListSubscribe, MultipleChoiceForm, UserPreferences,
    ListSubscriptionPolicyForm, ArchiveSettingsForm, MessageAcceptanceForm,
    DigestSettingsForm, AlterMessagesForm, ListAutomaticResponsesForm,
    ListIdentityForm, ListMassSubscription, ListMassRemoval, ListAddBanForm,
    ListHeaderMatchForm, ListHeaderMatchFormset, MemberModeration,
    DMARCMitigationsForm, ListAnonymousSubscribe)
from postorius.models import Domain, List, Mailman404Error, Style
from postorius.auth.decorators import (
    list_owner_required, list_moderator_required, superuser_required)
from postorius.auth.mixins import ListOwnerMixin
from postorius.views.generic import MailingListView


logger = logging.getLogger(__name__)


class ListMembersViews(ListOwnerMixin, MailingListView):

    # List of allowed roles for the memberships. The string value matches the
    # exact value Core's REST API expects.
    allowed_roles = ['owner', 'moderator', 'member', 'nonmember']

    def _prepare_query(self, request):
        """Prepare regex based query to search partial email addresses.

        Core's `members/find` API allows searching for memberships based on
        regex. This methods prepares a valid regex to pass on the the REST API.
        """
        if request.GET.get('q'):
            query = request.GET['q']
            if "*" not in query:
                query = '*{}*'.format(query)
        else:
            query = ''

        return query

    def get(self, request, list_id, role):
        """Handle GET for Member view.

        This includes all the membership roles (self.allowed_roles).
        """
        member_form = MemberForm()
        # If the role is misspelled, redirect to the default subscribers.
        if role not in self.allowed_roles:
            return redirect('list_members', list_id, 'member')
        context = dict()
        context['list'] = self.mailing_list
        context['role'] = role
        context['member_form'] = member_form
        context['page_title'] = _('List {}s'.format(role.capitalize()))
        context['query'] = self._prepare_query(request)

        def find_method(count, page):
            return self.mailing_list.find_members(
                context['query'], role=role, count=count, page=page)

        context['members'] = paginate(
            find_method,
            request.GET.get('page', 1),
            request.GET.get('count', 25),
            paginator_class=MailmanPaginator)
        context['page_subtitle'] = '({})'.format(
            context['members'].paginator.count)
        context['form_action'] = _('Add {}'.format(role))
        if context['query']:
            context['empty_error'] = _(
                'No {}s were found matching the search.'.format(role))
        else:
            context['empty_error'] = _('List has no {}s'.format(role))

        return render(request, 'postorius/lists/members.html', context)

    def _member_post(self, request, role):
        """Handle POST for members. Unsubscribe all the members selected."""
        form = MultipleChoiceForm(request.POST)
        if form.is_valid():
            members = form.cleaned_data['choices']
            for member in members:
                self.mailing_list.unsubscribe(member)
            messages.success(
                request,
                _('The selected members have been unsubscribed'))
        return redirect('list_members', self.mailing_list.list_id, role)

    def _non_member_post(self, request, role):
        """Handle POST for membership roles owner, moderator and non-member.

        Add memberships if the form is valid otherwise redirect to list_members
        page with an error message.
        """
        member_form = MemberForm(request.POST)
        if member_form.is_valid():
            try:
                self.mailing_list.add_role(
                    role=role,
                    address=member_form.cleaned_data['email'],
                    display_name=member_form.cleaned_data['display_name'])
                messages.success(
                    request,
                    _('%(email)s has been added with the role %(role)s'.format(
                        email=member_form.cleaned_data['email'],
                        role=role)))
            except HTTPError as e:
                messages.error(request, e.msg.decode())
        else:
            messages.error(request, member_form.errorsg)
        return redirect('list_members', self.mailing_list.list_id, role)

    def post(self, request, list_id, role=None):
        """Handle POST for list members page.

        List members page have more than one forms, depending on the membership
        type.

        - Regular subscribers have a MultipleChoiceForm, which returns a list
          of emails that needs to be unsubscribed from the MailingList. This is
          handled by :func:`self._member_post` method.

        - Owners, moderators and non-members have a MemberForm which allows
          adding new members with the given roles. This is handled by
          :func:`self._non_member_post` method.

        """
        if role not in self.allowed_roles:
            return redirect('list_members', list_id, 'member')

        if role in ('member'):
            return self._member_post(request, role)
        else:
            return self._non_member_post(request, role)


@login_required
@list_owner_required
def list_member_options(request, list_id, email):
    template_name = 'postorius/lists/memberoptions.html'
    mm_list = List.objects.get_or_404(fqdn_listname=list_id)
    try:
        mm_member = mm_list.find_members(address=email)[0]
        member_prefs = mm_member.preferences
    except (ValueError, IndexError):
        raise Http404(_('Member does not exist'))
    except Mailman404Error:
        return render(request, template_name, {'nolists': 'true'})
    initial_moderation = dict([
        (key, getattr(mm_member, key)) for key in MemberModeration.base_fields
        ])
    if request.method == 'POST':
        if request.POST.get("formname") == 'preferences':
            preferences_form = UserPreferences(
                request.POST, preferences=member_prefs)
            if preferences_form.is_valid():
                try:
                    preferences_form.save()
                except HTTPError as e:
                    messages.error(request, e.msg.decode())
                else:
                    messages.success(request, _("The member's preferences"
                                                " have been updated."))
            return redirect('list_member_options', list_id, email)
        elif request.POST.get("formname") == 'moderation':
            moderation_form = MemberModeration(
                request.POST, initial=initial_moderation)
            if moderation_form.is_valid():
                if not moderation_form.has_changed():
                    messages.info(
                        request, _("No change to the member's moderation."))
                    return redirect('list_member_options', list_id, email)
                for key in list(moderation_form.fields.keys()):
                    # In general, it would be a very bad idea to loop over the
                    # fields and try to set them one by one, However,
                    # moderation form has only one field.
                    setattr(mm_member, key, moderation_form.cleaned_data[key])
                try:
                    mm_member.save()
                except HTTPError as e:
                    messages.error(request, e.msg.decode())
                else:
                    messages.success(request, _("The member's moderation "
                                                "settings have been updated."))
                return redirect('list_member_options', list_id, email)
    else:
        preferences_form = UserPreferences(preferences=member_prefs)
        moderation_form = MemberModeration(initial=initial_moderation)
    return render(request, template_name, {
        'mm_member': mm_member,
        'list': mm_list,
        'preferences_form': preferences_form,
        'moderation_form': moderation_form,
        })


class ListSummaryView(MailingListView):
    """Shows common list metrics.
    """

    def get(self, request, list_id):
        data = {'list': self.mailing_list,
                'user_subscribed': False,
                'subscribed_address': None,
                'public_archive': False,
                'hyperkitty_enabled': False}
        if self.mailing_list.settings['archive_policy'] == 'public':
            data['public_archive'] = True
        if (getattr(settings, 'TESTING', False) and               # noqa: W504
                'hyperkitty' not in settings.INSTALLED_APPS):
            # avoid systematic test failure when HyperKitty is installed
            # (missing VCR request, see below).
            list(self.mailing_list.archivers)
        if ('hyperkitty' in settings.INSTALLED_APPS and           # noqa: W504
                'hyperkitty' in self.mailing_list.archivers and   # noqa: W504
                self.mailing_list.archivers['hyperkitty']):
            data['hyperkitty_enabled'] = True
        if request.user.is_authenticated:
            user_emails = EmailAddress.objects.filter(
                user=request.user, verified=True).order_by(
                "email").values_list("email", flat=True)
            pending_requests = [r['email'] for r in self.mailing_list.requests]
            for address in user_emails:
                if address in pending_requests:
                    data['user_request_pending'] = True
                    break
                try:
                    self.mailing_list.get_member(address)
                except ValueError:
                    pass
                else:
                    data['user_subscribed'] = True
                    data['subscribed_address'] = address
                    break  # no need to test more addresses
            data['subscribe_form'] = ListSubscribe(user_emails)
        else:
            user_emails = None
            data['anonymous_subscription_form'] = ListAnonymousSubscribe()
        return render(request, 'postorius/lists/summary.html', data)


class ChangeSubscriptionView(MailingListView):
    """Change mailing list subscription
    """

    @method_decorator(login_required)
    def post(self, request, list_id):
        try:
            user_emails = EmailAddress.objects.filter(
                user=request.user, verified=True).order_by(
                "email").values_list("email", flat=True)
            form = ListSubscribe(user_emails, request.POST)
            # Find the currently subscribed email
            old_email = None
            for address in user_emails:
                try:
                    self.mailing_list.get_member(address)
                except ValueError:
                    pass
                else:
                    old_email = address
                    break  # no need to test more addresses
            assert old_email is not None
            if form.is_valid():
                email = form.cleaned_data['email']
                if old_email == email:
                    messages.error(request, _('You are already subscribed'))
                else:
                    self.mailing_list.unsubscribe(old_email)
                    # Since the action is done via the web UI, no email
                    # confirmation is needed.
                    response = self.mailing_list.subscribe(
                        email, pre_confirmed=True)
                    if (type(response) == dict and                # noqa: W504
                            response.get('token_owner') == 'moderator'):
                        messages.success(
                            request, _('Your request to change the email for'
                                       ' this subscription was submitted and'
                                       ' is waiting for moderator approval.'))
                    else:
                        messages.success(request,
                                         _('Subscription changed to %s') %
                                         email)
            else:
                messages.error(request,
                               _('Something went wrong. Please try again.'))
        except HTTPError as e:
            messages.error(request, e.msg.decode())
        return redirect('list_summary', self.mailing_list.list_id)


class ListSubscribeView(MailingListView):
    """
    view name: `list_subscribe`
    """

    @method_decorator(login_required)
    def post(self, request, list_id):
        """
        Subscribes an email address to a mailing list via POST and
        redirects to the `list_summary` view.
        """
        try:
            user_emails = EmailAddress.objects.filter(
                user=request.user, verified=True).order_by(
                "email").values_list("email", flat=True)
            form = ListSubscribe(user_emails, request.POST)
            if form.is_valid():
                email = request.POST.get('email')
                response = self.mailing_list.subscribe(
                    email, pre_verified=True, pre_confirmed=True)
                if (type(response) == dict and                    # noqa: W504
                        response.get('token_owner') == 'moderator'):
                    messages.success(
                        request, _('Your subscription request has been'
                                   ' submitted and is waiting for moderator'
                                   ' approval.'))
                else:
                    messages.success(request,
                                     _('You are subscribed to %s.') %
                                     self.mailing_list.fqdn_listname)
            else:
                messages.error(request,
                               _('Something went wrong. Please try again.'))
        except HTTPError as e:
            messages.error(request, e.msg.decode())
        return redirect('list_summary', self.mailing_list.list_id)


class ListAnonymousSubscribeView(MailingListView):
    """
    view name: `list_anonymous_subscribe`
    """

    def post(self, request, list_id):
        """
        Subscribes an email address to a mailing list via POST and
        redirects to the `list_summary` view.
        This view is used for unauthenticated users and asks Mailman core to
        verify the supplied email address.
        """
        try:
            form = ListAnonymousSubscribe(request.POST)
            if form.is_valid():
                email = form.cleaned_data.get('email')
                self.mailing_list.subscribe(email, pre_verified=False,
                                            pre_confirmed=False)
                messages.success(request, _('Please check your inbox for '
                                            'further instructions'))
            else:
                messages.error(request,
                               _('Something went wrong. Please try again.'))
        except HTTPError as e:
            messages.error(request, e.msg.decode())
        return redirect('list_summary', self.mailing_list.list_id)


class ListUnsubscribeView(MailingListView):

    """Unsubscribe from a mailing list."""

    @method_decorator(login_required)
    def post(self, request, *args, **kwargs):
        email = request.POST['email']
        try:
            self.mailing_list.unsubscribe(email)
            messages.success(request, _('%s has been unsubscribed'
                                        ' from this list.') % email)
        except ValueError as e:
            messages.error(request, e)
        return redirect('list_summary', self.mailing_list.list_id)


@login_required
@list_owner_required
def list_mass_subscribe(request, list_id):
    mailing_list = List.objects.get_or_404(fqdn_listname=list_id)
    if request.method == 'POST':
        form = ListMassSubscription(request.POST)
        if form.is_valid():
            for data in form.cleaned_data['emails']:
                try:
                    # Parse the data to get the address and the display name
                    display_name, address = email.utils.parseaddr(data)
                    validate_email(address)
                    mailing_list.subscribe(address=address,
                                           display_name=display_name,
                                           pre_verified=True,
                                           pre_confirmed=True,
                                           pre_approved=True)
                    messages.success(
                        request, _('The address %(address)s has been'
                                   ' subscribed to %(list)s.') %
                        {'address': address,
                         'list': mailing_list.fqdn_listname})
                except HTTPError as e:
                    messages.error(request, e)
                except ValidationError:
                    messages.error(request, _('The email address %s'
                                              ' is not valid.') % address)
    else:
        form = ListMassSubscription()
    return render(request, 'postorius/lists/mass_subscribe.html',
                  {'form': form, 'list': mailing_list})


class ListMassRemovalView(MailingListView):

    """Class For Mass Removal"""

    @method_decorator(login_required)
    @method_decorator(list_owner_required)
    def get(self, request, *args, **kwargs):
        form = ListMassRemoval()
        return render(request, 'postorius/lists/mass_removal.html',
                      {'form': form, 'list': self.mailing_list})

    @method_decorator(list_owner_required)
    def post(self, request, *args, **kwargs):
        form = ListMassRemoval(request.POST)
        if not form.is_valid():
            messages.error(request, _('Please fill out the form correctly.'))
        else:
            for address in form.cleaned_data['emails']:
                try:
                    validate_email(address)
                    self.mailing_list.unsubscribe(address.lower())
                    messages.success(
                        request, _('The address %(address)s has been'
                                   ' unsubscribed from %(list)s.') %
                        {'address': address,
                         'list': self.mailing_list.fqdn_listname})
                except (HTTPError, ValueError) as e:
                    messages.error(request, e)
                except ValidationError:
                    messages.error(request, _('The email address %s'
                                              ' is not valid.') % address)
        return redirect('mass_removal', self.mailing_list.list_id)


def _perform_action(message_ids, action):
    for message_id in message_ids:
        action(message_id)


@login_required
@list_moderator_required
def list_moderation(request, list_id, held_id=-1):
    mailing_list = List.objects.get_or_404(fqdn_listname=list_id)
    if request.method == 'POST':
        form = MultipleChoiceForm(request.POST)
        if form.is_valid():
            message_ids = form.cleaned_data['choices']
            try:
                if 'accept' in request.POST:
                    _perform_action(message_ids, mailing_list.accept_message)
                    messages.success(request,
                                     _('The selected messages were accepted'))
                elif 'reject' in request.POST:
                    _perform_action(message_ids, mailing_list.reject_message)
                    messages.success(request,
                                     _('The selected messages were rejected'))
                elif 'discard' in request.POST:
                    _perform_action(message_ids, mailing_list.discard_message)
                    messages.success(request,
                                     _('The selected messages were discarded'))
            except HTTPError:
                messages.error(request, _('Message could not be found'))
    else:
        form = MultipleChoiceForm()
    held_messages = paginate(
        mailing_list.get_held_page,
        request.GET.get('page'), request.GET.get('count'),
        paginator_class=MailmanPaginator)
    context = {
        'list': mailing_list,
        'held_messages': held_messages,
        'form': form,
        }
    return render(request, 'postorius/lists/held_messages.html', context)


@login_required
@list_moderator_required
def moderate_held_message(request, list_id):
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
    msg_id = request.POST['msgid']
    mailing_list = List.objects.get_or_404(fqdn_listname=list_id)
    if 'accept' in request.POST:
        mailing_list.accept_message(msg_id)
        messages.success(request, _('The message was accepted'))
    elif 'reject' in request.POST:
        mailing_list.reject_message(msg_id)
        messages.success(request, _('The message was rejected'))
    elif 'discard' in request.POST:
        mailing_list.discard_message(msg_id)
        messages.success(request, _('The message was discarded'))
    return redirect('list_held_messages', list_id)


@login_required
@list_owner_required
def csv_view(request, list_id):
    """Export all the subscriber in csv
    """
    mm_lists = List.objects.get_or_404(fqdn_listname=list_id)

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = (
        'attachment; filename="Subscribers.csv"')

    writer = csv.writer(response)
    if mm_lists:
        for i in mm_lists.members:
            writer.writerow([i.email])

    return response


def _get_choosable_domains(request):
    domains = Domain.objects.all()
    return [(d.mail_host, d.mail_host) for d in domains]


def _get_choosable_styles(request):
    styles = Style.objects.all()
    options = [(style['name'], style['description'])
               for style in styles['styles']]
    # Reorder to put the default at the beginning
    for style_option in options:
        if style_option[0] == styles['default']:
            options.remove(style_option)
            options.insert(0, style_option)
            break
    return options


@login_required
@superuser_required
def list_new(request, template='postorius/lists/new.html'):
    """
    Add a new mailing list.
    If the request to the function is a GET request an empty form for
    creating a new list will be displayed. If the request method is
    POST the form will be evaluated. If the form is considered
    correct the list gets created and otherwise the form with the data
    filled in before the last POST request is returned. The user must
    be logged in to create a new list.
    """
    mailing_list = None
    choosable_domains = [('', _('Choose a Domain'))]
    choosable_domains += _get_choosable_domains(request)
    choosable_styles = _get_choosable_styles(request)
    if request.method == 'POST':
        form = ListNew(choosable_domains, choosable_styles, request.POST)
        if form.is_valid():
            # grab domain
            domain = Domain.objects.get_or_404(
                mail_host=form.cleaned_data['mail_host'])
            # creating the list
            try:
                mailing_list = domain.create_list(
                    form.cleaned_data['listname'],
                    style_name=form.cleaned_data['list_style'])
                mailing_list.add_owner(form.cleaned_data['list_owner'])
                list_settings = mailing_list.settings
                if form.cleaned_data['description']:
                    list_settings["description"] = \
                        form.cleaned_data['description']
                list_settings["advertised"] = form.cleaned_data['advertised']
                list_settings.save()
                messages.success(request, _("List created"))
                return redirect("list_summary",
                                list_id=mailing_list.list_id)
            # TODO catch correct Error class:
            except HTTPError as e:
                # Right now, there is no good way to detect that this is a
                # duplicate mailing list request other than checking the
                # reason for 400 error.
                if e.reason == b'Mailing list exists':
                    form.add_error(
                        'listname', _('Mailing List already exists.'))
                    return render(request, template, {'form': form})
                # Otherwise just render the generic error page.
                return render(request, 'postorius/errors/generic.html',
                              {'error': e.msg.decode()})
        else:
            messages.error(request, _('Please check the errors below'))
    else:
        form = ListNew(choosable_domains, choosable_styles, initial={
            'list_owner': request.user.email,
            'advertised': True,
            })
    return render(request, template, {'form': form})


def _unique_lists(lists):
    """Return unique lists from a list of mailing lists."""
    return {mlist.list_id: mlist for mlist in lists}.values()


@login_required
def list_index_authenticated(request):
    """Index page for authenticated users.

    Index page for authenticated users is slightly different than
    un-authenticated ones. Authenticated users will see all their memberships
    in the index page.

    This view is not paginated and will show all the lists.

    """
    role = request.GET.get('role', None)
    client = get_mailman_client()
    choosable_domains = _get_choosable_domains(request)

    # Get all the verified addresses of the user.
    user_emails = EmailAddress.objects.filter(
        user=request.user, verified=True).order_by(
            "email").values_list("email", flat=True)

    # Get all the mailing lists for the current user.
    all_lists = []
    for user_email in user_emails:
        try:
            all_lists.extend(client.find_lists(user_email, role=role))
        except HTTPError:
            # No lists exist with the given role for the given user.
            pass
    # If the user has no list that they are subscriber/owner/moderator of, we
    # just redirect them to the index page with all lists.
    if len(all_lists) == 0 and role is None:
        return redirect(reverse('list_index') + '?all-lists')
    # Render the list index page.
    context = {
        'lists': _unique_lists(all_lists),
        'domain_count': len(choosable_domains),
        'role': role
    }
    return render(
        request,
        'postorius/index.html',
        context
    )


def list_index(request, template='postorius/index.html'):
    """Show a table of all public mailing lists."""
    # TODO maxking: Figure out why does this view accept POST request and why
    # can't it be just a GET with list parameter.
    if request.method == 'POST':
        return redirect("list_summary", list_id=request.POST["list"])
    # If the user is logged-in, show them only related lists in the index,
    # except role is present in requests.GET.
    if request.user.is_authenticated and 'all-lists' not in request.GET:
        return list_index_authenticated(request)

    def _get_list_page(count, page):
        client = get_mailman_client()
        advertised = not request.user.is_superuser
        return client.get_list_page(
            advertised=advertised, count=count, page=page)

    lists = paginate(
        _get_list_page, request.GET.get('page'), request.GET.get('count'),
        paginator_class=MailmanPaginator)

    choosable_domains = _get_choosable_domains(request)

    return render(request, template,
                  {'lists': lists,
                   'all_lists': True,
                   'domain_count': len(choosable_domains)})


@login_required
@list_owner_required
def list_delete(request, list_id):
    """Deletes a list but asks for confirmation first.
    """
    the_list = List.objects.get_or_404(fqdn_listname=list_id)
    if request.method == 'POST':
        the_list.delete()
        return redirect("list_index")
    else:
        submit_url = reverse('list_delete',
                             kwargs={'list_id': list_id})
        cancel_url = reverse('list_index',)
        return render(request, 'postorius/lists/confirm_delete.html',
                      {'submit_url': submit_url, 'cancel_url': cancel_url,
                       'list': the_list})


@login_required
@list_moderator_required
def list_subscription_requests(request, list_id):
    """Shows a list of subscription requests.
    """
    m_list = List.objects.get_or_404(fqdn_listname=list_id)
    requests = [req
                for req in m_list.requests
                if req['token_owner'] == 'moderator']
    paginated_requests = paginate(
        requests,
        request.GET.get('page'),
        request.GET.get('count', 25))
    page_subtitle = '(%d)' % len(requests)
    return render(request, 'postorius/lists/subscription_requests.html',
                  {'list': m_list,
                   'paginated_requests': paginated_requests,
                   'page_subtitle': page_subtitle})


@login_required
@list_moderator_required
def handle_subscription_request(request, list_id, request_id, action):
    """
    Handle a subscription request. Possible actions:
        - accept
        - defer
        - reject
        - discard
    """
    confirmation_messages = {
        'accept': _('The request has been accepted.'),
        'reject': _('The request has been rejected.'),
        'discard': _('The request has been discarded.'),
        'defer': _('The request has been defered.'),
    }
    assert action in confirmation_messages
    try:
        m_list = List.objects.get_or_404(fqdn_listname=list_id)
        # Moderate request and add feedback message to session.
        m_list.moderate_request(request_id, action)
        messages.success(request, confirmation_messages[action])
    except HTTPError as e:
        if e.code == 409:
            messages.success(request,
                             _('The request was already moderated: %s')
                             % e.reason)
        else:
            messages.error(request, _('The request could not be moderated: %s')
                           % e.reason)
    return redirect('list_subscription_requests', m_list.list_id)


SETTINGS_SECTION_NAMES = (
    ('list_identity', _('List Identity')),
    ('automatic_responses', _('Automatic Responses')),
    ('alter_messages', _('Alter Messages')),
    ('dmarc_mitigations', _('DMARC Mitigations')),
    ('digest', _('Digest')),
    ('message_acceptance', _('Message Acceptance')),
    ('archiving', _('Archiving')),
    ('subscription_policy', _('Subscription Policy')),
)

SETTINGS_FORMS = {
    'list_identity': ListIdentityForm,
    'automatic_responses': ListAutomaticResponsesForm,
    'alter_messages': AlterMessagesForm,
    'dmarc_mitigations': DMARCMitigationsForm,
    'digest': DigestSettingsForm,
    'message_acceptance': MessageAcceptanceForm,
    'archiving': ArchiveSettingsForm,
    'subscription_policy': ListSubscriptionPolicyForm,
}


@login_required
@list_owner_required
def list_settings(request, list_id=None, visible_section=None,
                  template='postorius/lists/settings.html'):
    """
    View and edit the settings of a list.
    The function requires the user to be logged in and have the
    permissions necessary to perform the action.

    Use /<NAMEOFTHESECTION>/<NAMEOFTHEOPTION>
    to show only parts of the settings
    <param> is optional / is used to differ in between section and option might
    result in using //option
    """
    if visible_section is None:
        visible_section = 'list_identity'
    try:
        form_class = SETTINGS_FORMS[visible_section]
    except KeyError:
        raise Http404('No such settings section')
    m_list = List.objects.get_or_404(fqdn_listname=list_id)
    list_settings = m_list.settings
    initial_data = dict((key, value) for key, value in list_settings.items())
    # List settings are grouped an processed in different forms.
    if request.method == 'POST':
        form = form_class(request.POST, mlist=m_list, initial=initial_data)
        if form.is_valid():
            try:
                for key in form.changed_data:
                    if key in form_class.mlist_properties:
                        setattr(m_list, key, form.cleaned_data[key])
                    else:
                        list_settings[key] = form.cleaned_data[key]
                list_settings.save()
                messages.success(request, _('The settings have been updated.'))
            except HTTPError as e:
                messages.error(
                    request,
                    _('An error occured: ') + e.reason.decode('utf-8'))
            return redirect('list_settings', m_list.list_id, visible_section)
    else:
        form = form_class(initial=initial_data, mlist=m_list)

    return render(request, template, {
        'form': form,
        'section_names': SETTINGS_SECTION_NAMES,
        'list': m_list,
        'visible_section': visible_section,
        })


@login_required
@list_owner_required
def remove_role(request, list_id=None, role=None, address=None,
                template='postorius/lists/confirm_remove_role.html'):
    """Removes a list moderator or owner."""
    the_list = List.objects.get_or_404(fqdn_listname=list_id)
    redirect_on_success = redirect('list_members', the_list.list_id, role)
    roster = getattr(the_list, '{}s'.format(role))
    all_emails = [each.email for each in roster]
    if address not in all_emails:
        messages.error(request,
                       _('The user %(email)s is not in the %(role)s group')
                       % {'email': address, 'role': role})
        return redirect('list_members', the_list.list_id, role)

    if role == 'owner':
        if len(roster) == 1:
            messages.error(request, _('Removing the last owner is impossible'))
            return redirect('list_members', the_list.list_id, role)
        user_emails = EmailAddress.objects.filter(
            user=request.user, verified=True).order_by(
            "email").values_list("email", flat=True)
        if address in user_emails:
            # The user is removing themselves, redirect to the list info page
            # because they won't have access to the members page anyway.
            redirect_on_success = redirect('list_summary', the_list.list_id)

    if request.method == 'POST':
        try:
            the_list.remove_role(role, address)
        except HTTPError as e:
            messages.error(request, _('The user could not be removed: %(msg)s')
                           % {'msg': e.msg.decode()})
            return redirect('list_members', the_list.list_id, role)
        messages.success(request, _('The user %(address)s has been removed'
                                    ' from the %(role)s group.')
                         % {'address': address, 'role': role})
        return redirect_on_success
    return render(request, template,
                  {'role': role, 'address': address,
                   'list_id': the_list.list_id})


@login_required
@list_owner_required
def remove_all_subscribers(request, list_id):

    """Empty the list by unsubscribing all members."""

    mlist = List.objects.get_or_404(fqdn_listname=list_id)
    if len(mlist.members) == 0:
        messages.error(request,
                       _('No member is subscribed to the list currently.'))
        return redirect('mass_removal', mlist.list_id)
    if request.method == 'POST':
        try:
            # TODO maxking: This doesn't scale. Either the Core should provide
            # an endpoint to remove all subscribers or there should be some
            # better way to do this. Maybe, Core can take a list of email
            # addresses in batches of 50 and unsubscribe all of them.
            for names in mlist.members:
                mlist.unsubscribe(names.email)
            messages.success(request, _('All members have been'
                                        ' unsubscribed from the list.'))
            return redirect('list_members', mlist.list_id, 'subscriber')
        except Exception as e:
            messages.error(request, e)
    return render(request,
                  'postorius/lists/confirm_removeall_subscribers.html',
                  {'list': mlist})


@login_required
@list_owner_required
def list_bans(request, list_id):
    """
    Ban or unban email addresses.
    """
    # Get the list and cache the archivers property.
    m_list = List.objects.get_or_404(fqdn_listname=list_id)
    ban_list = m_list.bans

    # Process form submission.
    if request.method == 'POST':
        if 'add' in request.POST:
            addban_form = ListAddBanForm(request.POST)
            if addban_form.is_valid():
                try:
                    ban_list.add(addban_form.cleaned_data['email'])
                    messages.success(request, _(
                        'The email {} has been banned.'.format(
                            addban_form.cleaned_data['email'])))
                except HTTPError as e:
                    messages.error(
                        request, _('An error occured: %s') % e.reason)
                except ValueError as e:
                    messages.error(request, _('Invalid data: %s') % e)
                return redirect('list_bans', list_id)
        elif 'del' in request.POST:
            try:
                ban_list.remove(request.POST['email'])
                messages.success(request, _(
                    'The email {} has been un-banned'.format(
                        request.POST['email'])))
            except HTTPError as e:
                messages.error(request, _('An error occured: %s') % e.reason)
            except ValueError as e:
                messages.error(request, _('Invalid data: %s') % e)
            return redirect('list_bans', list_id)
    else:
        addban_form = ListAddBanForm()
    banned_addresses = paginate(
        list(ban_list), request.GET.get('page'), request.GET.get('count'))
    return render(request, 'postorius/lists/bans.html',
                  {'list': m_list,
                   'addban_form': addban_form,
                   'banned_addresses': banned_addresses,
                   })


@login_required
@list_owner_required
def list_header_matches(request, list_id):
    """
    View and edit the list's header matches.
    """
    m_list = List.objects.get_or_404(fqdn_listname=list_id)
    header_matches = m_list.header_matches
    HeaderMatchFormset = formset_factory(
        ListHeaderMatchForm, extra=1, can_delete=True, can_order=True,
        formset=ListHeaderMatchFormset)
    initial_data = [
        dict([
            (key, getattr(hm, key)) for key in ListHeaderMatchForm.base_fields
        ]) for hm in header_matches]

    # Process form submission.
    if request.method == 'POST':
        formset = HeaderMatchFormset(request.POST, initial=initial_data)
        if formset.is_valid():
            if not formset.has_changed():
                return redirect('list_header_matches', list_id)
            # Purge the existing header_matches
            header_matches.clear()
            # Add the ones in the form

            def form_order(f):
                # If ORDER is None (new header match), add it last.
                return f.cleaned_data.get('ORDER') or len(formset.forms)
            errors = []
            for form in sorted(formset, key=form_order):
                if 'header' not in form.cleaned_data:
                    # The new header match form was not filled
                    continue
                if form.cleaned_data.get('DELETE'):
                    continue
                try:
                    header_matches.add(
                        header=form.cleaned_data['header'],
                        pattern=form.cleaned_data['pattern'],
                        action=form.cleaned_data['action'],
                        )
                except HTTPError as e:
                    errors.append(e)
            for e in errors:
                messages.error(
                    request, _('An error occured: %s') % e.reason)
            if not errors:
                messages.success(request, _('The header matches were'
                                            ' successfully modified.'))
            return redirect('list_header_matches', list_id)
    else:
        formset = HeaderMatchFormset(initial=initial_data)
    # Adapt the last form to create new matches
    form_new = formset.forms[-1]
    form_new.fields['header'].widget.attrs['placeholder'] = _('New header')
    form_new.fields['pattern'].widget.attrs['placeholder'] = _('New pattern')
    del form_new.fields['ORDER']
    del form_new.fields['DELETE']

    return render(request, 'postorius/lists/header_matches.html', {
        'list': m_list,
        'formset': formset,
        })
