import base64
try:
    import cPickle as pickle
except ImportError:
    import pickle
import datetime
from math import log

try:
    from pygooglechart import SimpleLineChart
except ImportError:
    SimpleLineChart = None

from django.core.context_processors import csrf
from django.core.urlresolvers import reverse
from django.db.models import Count
from django.http import HttpResponse, HttpResponseForbidden, HttpResponseRedirect
from django.shortcuts import render_to_response
from django.template.loader import render_to_string
from django.utils import simplejson
from django.utils.datastructures import SortedDict
from django.utils.safestring import mark_safe
from django.views.decorators.csrf import csrf_protect

from sentry import settings
from sentry.helpers import FakeRequest, ImprovedExceptionReporter, get_filters
from sentry.models import GroupedMessage, Message
from sentry.templatetags.sentry_helpers import with_priority

def login_required(func):
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated():
            print "not authed"
            return HttpResponseRedirect(reverse('sentry-login'))
        if not request.user.has_perm('sentry_groupedmessage.can_view'):
            print "no perms"
            return HttpResponseRedirect(reverse('sentry-login'))
        return func(request, *args, **kwargs)
    wrapped.__doc__ = func.__doc__
    wrapped.__name__ = func.__name__
    return wrapped

@csrf_protect
def login(request):
    from django.contrib.auth import authenticate, login as login_
    from django.contrib.auth.forms import AuthenticationForm
    
    if request.POST:
        form = AuthenticationForm(request, request.POST)
        if form.is_valid():
            login_(request, form.get_user())
            return HttpResponseRedirect(request.POST.get('next') or reverse('sentry'))
        else:
            request.session.set_test_cookie()
    else:
        form = AuthenticationForm(request)
        request.session.set_test_cookie()

    
    context = locals()
    context.update(csrf(request))
    return render_to_response('sentry/login.html', locals(), )

@login_required
def index(request):
    filters = []
    for filter_ in get_filters():
        filters.append(filter_(request))
    
    try:
        page = int(request.GET.get('p', 1))
    except (TypeError, ValueError):
        page = 1

    # this only works in postgres
    message_list = GroupedMessage.objects.filter(
        status=0,
    ).extra(
        select={
            'score': GroupedMessage.get_score_clause(),
        }
    ).order_by('-score', '-last_seen').distinct()
    
    any_filter = False
    for filter_ in filters:
        if not filter_.is_set():
            continue
        any_filter = True
        message_list = filter_.get_query_set(message_list)
    
    today = datetime.datetime.now()

    if not any_filter and page == 1:
        realtime = True
    else:
        realtime = False
    
    return render_to_response('sentry/index.html', locals())

@login_required
def ajax_handler(request):
    op = request.REQUEST.get('op')
    if op == 'poll':
        logger_names = SortedDict((l, l) for l in GroupedMessage.objects.values_list('logger', flat=True).distinct())
        server_names = SortedDict((l, l) for l in Message.objects.values_list('server_name', flat=True).distinct())
        level_names = SortedDict((str(k), v) for k, v in settings.LOG_LEVELS)

        logger = request.GET.get('logger')
        server_name = request.GET.get('server_name') or ''
        level = request.GET.get('level') or ''

        if logger not in logger_names:
            logger = ''

        if server_name not in server_names:
            server_name = ''

        if level not in level_names:
            level = ''
        
        message_list = GroupedMessage.objects.filter(
            status=0,
        ).extra(
            select={
                'score': GroupedMessage.get_score_clause(),
            }
        ).order_by('-score', '-last_seen')

        if logger:
            message_list = message_list.filter(logger=logger)

        if level:
            message_list = message_list.filter(level=level)

        if server_name:
            message_list = message_list.filter(message_set__server_name=server_name).distinct()
        
        data = [
            (m.pk, {
                'html': render_to_string('sentry/partial/_group.html', {'group': m, 'priority': p}),
                'count': m.times_seen,
                'priority': p,
            }) for m, p in with_priority(message_list[0:15])]

    elif op == 'resolve':
        gid = request.REQUEST.get('gid')
        if not gid:
            return HttpResponseForbidden()
        try:
            group = GroupedMessage.objects.get(pk=gid)
        except GroupedMessage.DoesNotExist:
            return HttpResponseForbidden()
        
        GroupedMessage.objects.filter(pk=group.pk).update(status=1)
        group.status = 1
        
        if not request.is_ajax():
            return HttpResponseRedirect(request.META['HTTP_REFERER'])
        
        data = [
            (m.pk, {
                'html': render_to_string('sentry/partial/_group.html', {'group': m}),
                'count': m.times_seen,
            }) for m in [group]]
        
    response = HttpResponse(simplejson.dumps(data))
    response['Content-Type'] = 'application/json'
    return response

@login_required
def group(request, group_id):
    group = GroupedMessage.objects.get(pk=group_id)

    message_list = group.message_set.all()
    
    obj = message_list[0]
    if '__sentry__' in obj.data:
        module, args, frames = obj.data['__sentry__']['exc']
        obj.class_name = str(obj.class_name)
        # We fake the exception class due to many issues with imports/builtins/etc
        exc_type = type(obj.class_name, (Exception,), {})
        exc_value = exc_type(obj.message)

        exc_value.args = args
    
        fake_request = FakeRequest()
        fake_request.META = obj.data.get('META', {})
        fake_request.GET = obj.data.get('GET', {})
        fake_request.POST = obj.data.get('POST', {})
        fake_request.FILES = obj.data.get('FILES', {})
        fake_request.COOKIES = obj.data.get('COOKIES', {})
        fake_request.url = obj.url
        if obj.url:
            fake_request.path_info = '/' + obj.url.split('/', 3)[-1]
        else:
            fake_request.path_info = ''

        reporter = ImprovedExceptionReporter(fake_request, exc_type, exc_value, frames, obj.data['__sentry__'].get('template'))
        traceback = mark_safe(reporter.get_traceback_html())
    else:
        traceback = mark_safe('<pre>%s</pre>' % (group.traceback,))
    
    unique_urls = message_list.filter(url__isnull=False).values_list('url', 'logger', 'view', 'checksum').annotate(times_seen=Count('url')).values('url', 'times_seen').order_by('-times_seen')
    
    unique_servers = message_list.filter(server_name__isnull=False).values_list('server_name', 'logger', 'view', 'checksum').annotate(times_seen=Count('server_name')).values('server_name', 'times_seen').order_by('-times_seen')
    
    if SimpleLineChart:
        today = datetime.datetime.now()

        chart_qs = message_list\
                          .filter(datetime__gte=today - datetime.timedelta(hours=24))\
                          .extra(select={'hour': 'extract(hour from datetime)'}).values('hour')\
                          .annotate(num=Count('id')).values_list('hour', 'num')

        rows = dict(chart_qs)
        if rows:
            max_y = max(rows.values())
        else:
            max_y = 1

        chart = SimpleLineChart(384, 80, y_range=[0, max_y])
        chart.add_data([max_y]*30)
        chart.add_data([rows.get((today-datetime.timedelta(hours=d)).hour, 0) for d in range(0, 24)][::-1])
        chart.add_data([0]*30)
        chart.fill_solid(chart.BACKGROUND, 'eeeeee')
        chart.add_fill_range('eeeeee', 0, 1)
        chart.add_fill_range('e0ebff', 1, 2)
        chart.set_colours(['eeeeee', '999999', 'eeeeee'])
        chart.set_line_style(1, 1)
        chart_url = chart.get_url()
    
    return render_to_response('sentry/group.html', locals())

def store(request):
    key = request.POST.get('key')
    if key != settings.KEY:
        return HttpResponseForbidden('Invalid credentials')
    
    data = request.POST.get('data')
    if not data:
        return HttpResponseForbidden('Missing data')
    
    try:
        data = base64.b64decode(pickle.loads(data.decode('zlib')))
    except:
        return HttpResponseForbidden('Bad data')

    GroupedMessage.objects._create(**data)
    
    return HttpResponse()