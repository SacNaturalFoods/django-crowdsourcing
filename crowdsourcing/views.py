from __future__ import absolute_import

from datetime import datetime
import httplib
from itertools import count
import logging
import smtplib

from django.conf import settings
from django.core.exceptions import FieldError
from django.core.mail import EmailMultiAlternatives
from django.core.paginator import Paginator, EmptyPage, InvalidPage
from django.core.urlresolvers import reverse, NoReverseMatch
from django.db.models import Q
from django.http import HttpResponse, HttpResponseRedirect, Http404
from django.shortcuts import get_object_or_404, render_to_response
from django.template import RequestContext as _rc
from django.utils.html import escape

from .forms import forms_for_survey
from .models import (
    Answer,
    OPTION_TYPE_CHOICES,
    Question,
    SURVEY_DISPLAY_TYPE_CHOICES,
    Submission,
    Survey,
    SurveyReport,
    SurveyReportDisplay,
    extra_from_filters,
    get_all_answers,
    get_filters)
from .jsonutils import dump, dumps

from .util import ChoiceEnum, get_function
from . import settings as local_settings


def _user_entered_survey(request, survey):
    return bool(survey.submissions_for(
        request.user,
        request.session.session_key.lower()).count())


def _entered_no_more_allowed(request, survey):
    """ The user entered the survey and the survey allows only one entry. """
    return all((
        not survey.allow_multiple_submissions,
        _user_entered_survey(request, survey),))


def _get_remote_ip(request):
    forwarded=request.META.get('HTTP_X_FORWARDED_FOR')
    if forwarded:
        return forwarded.split(',')[-1].strip()
    return request.META['REMOTE_ADDR']


def _login_url(request):
    if local_settings.LOGIN_VIEW:
        return reverse(local_settings.LOGIN_VIEW) + '?next=%s' % request.path
    return "/?login_required=true"


def _get_survey_or_404(slug):
    return get_object_or_404(Survey.live, slug=slug)


def _survey_submit(request, survey):
    if survey.require_login and request.user.is_anonymous():
        # again, the form should only be shown after the user is logged in, but
        # to be safe...
        return HttpResponseRedirect(_login_url(request))
    if not hasattr(request, 'session'):
        return HttpResponse("Cookies must be enabled to use this application.",
                            status=httplib.FORBIDDEN)
    if (_entered_no_more_allowed(request, survey)):
        slug_template = 'crowdsourcing/%s_already_submitted.html' % survey.slug
        return render_to_response([slug_template,
                                   'crowdsourcing/already_submitted.html'],
                                  dict(survey=survey),
                                  _rc(request))

    forms = forms_for_survey(survey, request)

    if all(form.is_valid() for form in forms):
        submission_form = forms[0]
        submission = submission_form.save(commit=False)
        submission.survey = survey
        submission.ip_address = _get_remote_ip(request)
        submission.is_public = not survey.moderate_submissions
        if request.user.is_authenticated():
            submission.user = request.user
        submission.save()
        for form in forms[1:]:
            answer = form.save(commit=False)
            if isinstance(answer, (list, tuple)):
                for a in answer:
                    a.submission=submission
                    a.save()
            else:
                if answer:
                    answer.submission=submission
                    answer.save()
        # go to survey results/thanks page
        if survey.email:
            _send_survey_email(request, survey, submission)
        if survey.can_have_public_submissions():
            return _survey_results_redirect(request, survey, thanks=True)
        return _survey_show_form(request, survey, ())
    else:
        return _survey_show_form(request, survey, forms)


def _url_for_edit(request, obj):
    view_args = (obj._meta.app_label, obj._meta.module_name,)
    try:
        edit_url = reverse("admin:%s_%s_change" % view_args, args=(obj.id,))
    except NoReverseMatch:
        # Probably 'admin' is not a registered namespace on a site without an
        # admin. Just fake it.
        edit_url = "/admin/%s/%s/%d/" % (view_args + (obj.id,))
    admin_url = local_settings.SURVEY_ADMIN_SITE
    if not admin_url:
        admin_url = "http://" + request.META["HTTP_HOST"]
    elif len(admin_url) < 4 or admin_url[:4].lower() != "http":
        admin_url = "http://" + admin_url
    return admin_url + edit_url


def _send_survey_email(request, survey, submission):
    subject = survey.title
    sender = local_settings.SURVEY_EMAIL_FROM
    links = [(_url_for_edit(request, submission), "Edit Submission"),
             (_url_for_edit(request, survey), "Edit Survey"),]
    if survey.can_have_public_submissions():
        u = "http://" + request.META["HTTP_HOST"] + _survey_report_url(survey)
        links.append((u, "View Survey",))
    parts = ["<a href=\"%s\">%s</a>" % link for link in links]
    set = submission.answer_set.all()
    lines = ["%s: %s" % (a.question.label, escape(a.value),) for a in set]
    parts.extend(lines)
    html_email = "<br/>\n".join(parts)
    recipients = [a.strip() for a in survey.email.split(",")]
    email_msg = EmailMultiAlternatives(subject,
                                       html_email,
                                       sender,
                                       recipients)
    email_msg.attach_alternative(html_email, 'text/html')
    try:
        email_msg.send()
    except smtplib.SMTPException as ex:
        logging.exception("SMTP error sending email: %s" % str(ex))
    except Exception as ex:
        logging.exception("Unexpected error sending email: %s" % str(ex))


def _survey_show_form(request, survey, forms):
    specific_template = 'crowdsourcing/%s_survey_detail.html' % survey.slug
    entered = _user_entered_survey(request, survey)
    return render_to_response([specific_template,
                               'crowdsourcing/survey_detail.html'],
                              dict(survey=survey,
                                   forms=forms,
                                   entered=entered,
                                   login_url=_login_url(request)),
                              _rc(request))


def _can_show_form(request, survey):
    authenticated = request.user.is_authenticated()
    return all((
        survey.is_open,
        authenticated or not survey.require_login,
        not _entered_no_more_allowed(request, survey)))


def survey_detail(request, slug):
    """ When you load the survey, this view decides what to do. It displays
    the form, redirects to the results page, displays messages, or whatever
    makes sense based on the survey, the user, and the user's entries. """
    survey = _get_survey_or_404(slug)
    if not survey.is_open and survey.can_have_public_submissions():
        return _survey_results_redirect(request, survey)
    need_login = (survey.is_open
                  and survey.require_login
                  and not request.user.is_authenticated())
    if _can_show_form(request, survey):
        if request.method == 'POST':
            return _survey_submit(request, survey)
        forms = forms_for_survey(survey, request)
    elif need_login:
        forms = ()
    elif survey.can_have_public_submissions():
        return _survey_results_redirect(request, survey)
    else: # Survey is closed with private results.
        forms = ()
    return _survey_show_form(request, survey, forms)


def _survey_results_redirect(request, survey, thanks=False):
    response = HttpResponseRedirect(_survey_report_url(survey))
    if thanks:
        request.session['survey_thanks_%s' % survey.slug] = '1'
    return response


def _survey_report_url(survey):
    return reverse('survey_default_report_page_1',
                   kwargs={'slug': survey.slug})


def allowed_actions(request, slug):
    survey = _get_survey_or_404(slug)
    response = HttpResponse(mimetype='application/json')
    dump({"enter": _can_show_form(request, survey),
          "view": survey.can_have_public_submissions()}, response)
    return response


def questions(request, slug):
    response = HttpResponse(mimetype='application/json')
    dump(_get_survey_or_404(slug).to_jsondata(), response)
    return response


def submissions(request):
    """ Use this view to make arbitrary queries on submissions. Use the query
    string to pass keys and values. For example,
    /crowdsourcing/submissions/?survey=my-survey will return all submissions
    for the survey with slug my-survey.
    survey - the slug for the survey
    user - the username of the submittor. Leave blank for submissions without
        a logged in user.
    submitted_from and submitted_to - strings in the format YYYY-mm-ddThh:mm:ss
        For example, 2010-04-05T13:02:03
    featured - A blank value, 'f', 'false', 0, 'n', and 'no' all mean ignore 
        the featured flag. Everything else means display only featured. """
    response = HttpResponse(mimetype='application/json')
    results = Submission.objects.filter(is_public=True)
    valid_filters = (
        'survey',
        'user',
        'submitted_from',
        'submitted_to',
        'featured')
    for field in request.GET.keys():
        if field in valid_filters:
            value = request.GET[field]
            if 'survey' == field:
                field = 'survey__slug'
            elif 'user' == field:
                if '' == value:
                    field = 'user'
                    value = None
                else:
                    field = 'user__username'
            elif field in ('submitted_from', 'submitted_to'):
                format = "%Y-%m-%dT%H:%M:%S"
                try:
                    value = datetime.strptime(value, format)
                except ValueError:
                    return HttpResponse(
                        ("Invalid %s format. Try, for example, "
                         "%s") % (field, datetime.now().strftime(format),))
                if 'submitted_from' == field:
                    field = 'submitted_at__gte'
                else:
                    field = 'submitted_at__lte'
            elif 'featured' == field:
                falses = ('f', 'false', 'no', 'n', '0',)
                value = len(value) and not value.lower() in falses
            # field is unicode but needs to be ascii.
            results = results.filter(**{str(field): value})
        else:
            return HttpResponse(("You can't filter on %s. Valid options are "
                                 "%s.") % (field, valid_filters))
    dump([result.to_jsondata() for result in results], response)
    return response


def submission(request, id):
    template = 'crowdsourcing/submission.html'
    sub = get_object_or_404(Submission.objects, is_public=True, pk=id)
    return render_to_response(template, dict(submission=sub), _rc(request))
    

def _default_report(survey):
    field_count = count(1)
    pie_choices = (
        OPTION_TYPE_CHOICES.BOOL,
        OPTION_TYPE_CHOICES.SELECT,
        OPTION_TYPE_CHOICES.CHOICE,
        OPTION_TYPE_CHOICES.NUMERIC_SELECT,
        OPTION_TYPE_CHOICES.NUMERIC_CHOICE)
    all_choices = pie_choices + (OPTION_TYPE_CHOICES.LOCATION,)
    public_fields = survey.get_public_fields()
    fields = [f for f in public_fields if f.option_type in all_choices]
    report = SurveyReport(
        survey=survey,
        title=survey.title,
        summary=survey.description or survey.tease)
    displays = []
    for field in fields:
        if field.option_type in pie_choices:
            type = SURVEY_DISPLAY_TYPE_CHOICES.PIE
        elif field.option_type == OPTION_TYPE_CHOICES.LOCATION:
            type = SURVEY_DISPLAY_TYPE_CHOICES.MAP
        displays.append(SurveyReportDisplay(
            report=report,
            display_type=type,
            fieldnames=field.fieldname,
            annotation=field.label,
            order=field_count.next()))
    report.survey_report_displays = displays
    return report


def survey_report(request, slug, report='', page=None):
    templates = ['crowdsourcing/survey_report_%s.html' % slug,
                 'crowdsourcing/survey_report.html']
    return _survey_report(request, slug, report, page, templates)


def embeded_survey_report(request, slug, report=''):
    templates = ['crowdsourcing/embeded_survey_report_%s.html' % slug,
                 'crowdsourcing/embeded_survey_report.html']
    return _survey_report(request, slug, report, None, templates)


def _survey_report(request, slug, report, page, templates):
    """ Show a report for the survey. As rating is done in a separate
    application we don't directly check request.GET["sort"] here.
    local_settings.PRE_REPORT is the place for that. """
    if page is None:
        page = 1
    else:
        try:
            page = int(page)
        except ValueError:
            raise Http404
    survey = _get_survey_or_404(slug)
    # is the survey anything we can actually have a report on?
    if not survey.can_have_public_submissions():
        raise Http404
    reports = survey.surveyreport_set.all()
    if report:
        report_obj = get_object_or_404(reports, slug=report)
    elif survey.default_report:
        args = {"slug": survey.slug, "report": survey.default_report.slug}
        return HttpResponseRedirect(reverse("survey_report_page_1",
                                    kwargs=args))
    else:
        report_obj = _default_report(survey)

    archive_fields = list(survey.get_public_archive_fields())
    fields = list(survey.get_public_fields())
    filters = get_filters(survey, request.GET)

    public = survey.public_submissions()
    id_field = "crowdsourcing_submission.id"
    submissions = extra_from_filters(public, id_field, survey, request.GET)
    # If you want to sort based on rating, wire it up here.
    if local_settings.PRE_REPORT:
        pre_report = get_function(local_settings.PRE_REPORT)
        submissions = pre_report(
            submissions=submissions,
            report=report_obj,
            request=request)

    ids = None
    if report_obj.limit_results_to:
        submissions = submissions[:report_obj.limit_results_to]
        ids = ",".join([str(s.pk) for s in submissions])
    if not report_obj.display_individual_results:
        submissions = submissions.none()
    paginator, page_obj = paginate_or_404(submissions, page)

    page_answers = get_all_answers(page_obj.object_list)

    pages_to_link = []
    for i in range(page - 5, page + 5):
        if 1 <= i <= paginator.num_pages:
            pages_to_link.append(i)
    if pages_to_link[0] > 1:
        pages_to_link = [1, False] + pages_to_link
    if pages_to_link[-1] < paginator.num_pages:
        pages_to_link = pages_to_link + [False, paginator.num_pages]

    context = dict(
        survey=survey,
        submissions=submissions,
        paginator=paginator,
        page_obj=page_obj,
        ids=ids,
        pages_to_link=pages_to_link,
        fields=fields,
        archive_fields=archive_fields,
        filters=filters,
        report=report_obj,
        page_answers=page_answers,
        request=request)
    
    return render_to_response(templates, context, _rc(request))


def paginate_or_404(queryset, page, num_per_page=20):
    """
    paginate a queryset (or other iterator) for the given page, returning the
    paginator and page object. Raises a 404 for an invalid page.
    """
    if page is None:
        page = 1
    paginator = Paginator(queryset, num_per_page)
    try:
        page_obj = paginator.page(page)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)
    except InvalidPage:
        raise Http404
    return paginator, page_obj


def location_question_results(
    request,
    question_id,
    submission_ids=None,
    limit_map_answers=None):
    question = get_object_or_404(Question.objects.select_related("survey"),
                                 pk=question_id,
                                 answer_is_public=True)
    if not question.survey.can_have_public_submissions():
        raise Http404
    icon_lookup = {}
    icon_questions = question.survey.icon_questions()
    for icon_question in icon_questions:
        icon_by_answer = {}
        for (option, icon) in icon_question.parsed_option_icon_pairs():
            if icon:
                icon_by_answer[option] = icon
        for answer in icon_question.answer_set.all():
            if answer.value in icon_by_answer:
                icon = icon_by_answer[answer.value]
                icon_lookup[answer.submission_id] = icon

    answers = question.answer_set.filter(
        ~Q(latitude=None),
        ~Q(longitude=None),
        submission__is_public=True)
    answers = extra_from_filters(
        answers,
        "submission_id",
        question.survey,
        request.GET)
    if submission_ids:
        answers = answers.filter(submission__in=submission_ids.split(","))
    if limit_map_answers:
        answers = answers[:limit_map_answers]
    entries = []
    view = "crowdsourcing.views.submission_for_map"
    for answer in answers:
        kwargs = {"id": answer.submission_id}
        d = {
            "lat": answer.latitude,
            "lng": answer.longitude,
            "url": reverse(view, kwargs=kwargs)}
        if answer.submission_id in icon_lookup:
            d["icon"] = icon_lookup[answer.submission_id]
        entries.append(d)
    response = HttpResponse(mimetype='application/json')
    dump({"entries": entries}, response)
    return response


def submission_for_map(request, id):
    template = 'crowdsourcing/submission_for_map.html'
    sub = get_object_or_404(Submission.objects, is_public=True, pk=id)
    return render_to_response(template, dict(submission=sub), _rc(request))
