import datetime
import json
import socket
import unittest

from http.server import HTTPServer
from io import BytesIO as IO
from socketserver import ThreadingMixIn
from unittest.mock import Mock, patch, PropertyMock

from patroni import global_config
from patroni.api import RestApiHandler, RestApiServer
from patroni.dcs import ClusterConfig, Member
from patroni.exceptions import PostgresConnectionException
from patroni.ha import _MemberStatus
from patroni.postgresql.config import get_param_diff
from patroni.postgresql.misc import PostgresqlRole, PostgresqlState
from patroni.psycopg import OperationalError
from patroni.utils import RetryFailedError, tzutc

from . import MockConnect, psycopg_connect
from .test_etcd import socket_getaddrinfo
from .test_ha import get_cluster_initialized_without_leader

future_restart_time = datetime.datetime.now(tzutc) + datetime.timedelta(days=5)
postmaster_start_time = datetime.datetime.now(tzutc)


class MockConnection:

    @staticmethod
    def get(*args):
        return psycopg_connect()

    @staticmethod
    def query(sql, *params):
        return [(postmaster_start_time, 0, '', 0, '', False, postmaster_start_time, 1, 'streaming', None, 0,
                 '[{"application_name":"walreceiver","client_addr":"1.2.3.4",'
                 + '"state":"streaming","sync_state":"async","sync_priority":0}]')]


class MockConnectionPool:

    @staticmethod
    def get(*args):
        return MockConnection()


class MockPostgresql:

    connection_pool = MockConnectionPool()
    name = 'test'
    state = PostgresqlState.RUNNING
    role = PostgresqlRole.PRIMARY
    server_version = 90625
    major_version = 90600
    sysid = 'dummysysid'
    scope = 'dummy'
    pending_restart_reason = {}
    wal_name = 'wal'
    lsn_name = 'lsn'
    wal_flush = '_flush'
    POSTMASTER_START_TIME = 'pg_catalog.pg_postmaster_start_time()'
    TL_LSN = 'CASE WHEN pg_catalog.pg_is_in_recovery()'
    mpp_handler = Mock()

    @staticmethod
    def postmaster_start_time():
        return postmaster_start_time

    @staticmethod
    def replica_cached_timeline(_):
        return 2

    @staticmethod
    def is_running():
        return True

    @staticmethod
    def replication_state_from_parameters(*args):
        return 'streaming'


class MockWatchdog(object):
    is_healthy = False


class MockHa(object):

    state_handler = MockPostgresql()
    watchdog = MockWatchdog()

    @staticmethod
    def update_failsafe(*args):
        return 'foo'

    @staticmethod
    def failsafe_is_active(*args):
        return True

    @staticmethod
    def is_leader():
        return False

    @staticmethod
    def reinitialize(_):
        return 'reinitialize'

    @staticmethod
    def restart(*args, **kwargs):
        return (True, '')

    @staticmethod
    def restart_scheduled():
        return False

    @staticmethod
    def delete_future_restart():
        return True

    @staticmethod
    def fetch_nodes_statuses(members):
        return [_MemberStatus(None, True, None, 0, {})]

    @staticmethod
    def schedule_future_restart(data):
        return True

    @staticmethod
    def is_lagging(wal):
        return False

    @staticmethod
    def get_effective_tags():
        return {'nosync': True}

    @staticmethod
    def wakeup():
        pass

    @staticmethod
    def is_paused():
        return True


class MockLogger(object):

    NORMAL_LOG_QUEUE_SIZE = 2
    queue_size = 3
    records_lost = 1


class MockPatroni(object):

    ha = MockHa()
    postgresql = ha.state_handler
    dcs = Mock()
    logger = MockLogger()
    tags = {"key1": True, "key2": False, "key3": 1, "key4": 1.4, "key5": "RandomTag"}
    version = '0.00'
    noloadbalance = PropertyMock(return_value=False)
    scheduled_restart = {'schedule': future_restart_time,
                         'postmaster_start_time': postgresql.postmaster_start_time()}

    @staticmethod
    def sighup_handler():
        pass

    @staticmethod
    def api_sigterm():
        pass


class MockRequest(object):

    def __init__(self, request):
        self.request = request.encode('utf-8')

    def makefile(self, *args, **kwargs):
        return IO(self.request)

    def sendall(self, *args, **kwargs):
        pass


class MockRestApiServer(RestApiServer):

    def __init__(self, Handler, request, config=None):
        self.socket = 0
        self.serve_forever = Mock()
        MockRestApiServer._BaseServer__is_shut_down = Mock()
        MockRestApiServer._BaseServer__shutdown_request = True
        config = config or {'listen': '127.0.0.1:8008', 'auth': 'test:test', 'certfile': 'dumb', 'verify_client': 'a',
                            'http_extra_headers': {'foo': 'bar'}, 'https_extra_headers': {'foo': 'sbar'}}
        super(MockRestApiServer, self).__init__(MockPatroni(), config)
        Handler(MockRequest(request), ('0.0.0.0', 8080), self)


@patch('ssl.SSLContext.load_cert_chain', Mock())
@patch('ssl.SSLContext.wrap_socket', Mock(return_value=0))
@patch.object(HTTPServer, '__init__', Mock())
class TestRestApiHandler(unittest.TestCase):

    _authorization = '\nAuthorization: Basic dGVzdDp0ZXN0'

    def test_do_GET(self):
        MockPostgresql.pending_restart_reason = {'max_connections': get_param_diff('200', '100')}
        MockPatroni.dcs.cluster.status.last_lsn = 20
        with patch.object(global_config.__class__, 'is_synchronous_mode', PropertyMock(return_value=True)):
            MockRestApiServer(RestApiHandler, 'GET /replica')
        MockRestApiServer(RestApiHandler, 'GET /replica?lag=1M')
        MockRestApiServer(RestApiHandler, 'GET /replica?lag=10MB')
        MockRestApiServer(RestApiHandler, 'GET /replica?lag=10485760')
        MockRestApiServer(RestApiHandler, 'GET /read-only')
        with patch.object(RestApiHandler, 'get_postgresql_status', Mock(return_value={})):
            MockRestApiServer(RestApiHandler, 'GET /replica')
        with patch.object(RestApiHandler, 'get_postgresql_status', Mock(return_value={'role': PostgresqlRole.PRIMARY})):
            MockRestApiServer(RestApiHandler, 'GET /replica')
        with patch.object(RestApiHandler, 'get_postgresql_status',
                          Mock(return_value={'state': PostgresqlState.RUNNING})):
            MockRestApiServer(RestApiHandler, 'GET /health')
        MockRestApiServer(RestApiHandler, 'GET /leader')
        with patch.object(RestApiHandler, 'get_postgresql_status',
                          Mock(return_value={'role': PostgresqlRole.REPLICA, 'sync_standby': True})):
            MockRestApiServer(RestApiHandler, 'GET /synchronous')
            MockRestApiServer(RestApiHandler, 'GET /read-only-sync')
        with patch.object(RestApiHandler, 'get_postgresql_status',
                          Mock(return_value={'role': PostgresqlRole.REPLICA, 'quorum_standby': True})):
            MockRestApiServer(RestApiHandler, 'GET /quorum')
            MockRestApiServer(RestApiHandler, 'GET /read-only-quorum')
        with patch.object(RestApiHandler, 'get_postgresql_status',
                          Mock(return_value={'role': PostgresqlRole.REPLICA})):
            MockRestApiServer(RestApiHandler, 'GET /asynchronous')
        with patch.object(MockHa, 'is_leader', Mock(return_value=True)):
            MockRestApiServer(RestApiHandler, 'GET /replica')
            MockRestApiServer(RestApiHandler, 'GET /read-only-sync')
            MockRestApiServer(RestApiHandler, 'GET /read-only-quorum')
            with patch.object(global_config.__class__, 'is_standby_cluster', Mock(return_value=True)):
                MockRestApiServer(RestApiHandler, 'GET /standby_leader')
        MockPatroni.dcs.cluster = None
        with patch.object(RestApiHandler, 'get_postgresql_status', Mock(return_value={'role': PostgresqlRole.PRIMARY})):
            MockRestApiServer(RestApiHandler, 'GET /primary')
        with patch.object(MockHa, 'restart_scheduled', Mock(return_value=True)):
            MockRestApiServer(RestApiHandler, 'GET /primary')
        self.assertIsNotNone(MockRestApiServer(RestApiHandler, 'GET /primary'))
        with patch.object(RestApiServer, 'query',
                          Mock(return_value=[('', 1, '', '', '', '', False, None, 0, None, 0, '')])):
            self.assertIsNotNone(MockRestApiServer(RestApiHandler, 'GET /patroni'))
        with patch.object(global_config.__class__, 'is_standby_cluster', Mock(return_value=True)), \
                patch.object(global_config.__class__, 'is_paused', Mock(return_value=True)):
            MockRestApiServer(RestApiHandler, 'GET /standby_leader')

        # test tags
        #
        MockRestApiServer(RestApiHandler, 'GET /primary?lag=1M&'
                                          'tag_key1=true&tag_key2=false&'
                                          'tag_key3=1&tag_key4=1.4&tag_key5=RandomTag')
        MockRestApiServer(RestApiHandler, 'GET /primary?lag=1M&'
                                          'tag_key1=true&tag_key2=False&'
                                          'tag_key3=1&tag_key4=1.4&tag_key5=RandomTag')
        MockRestApiServer(RestApiHandler, 'GET /primary?lag=1M&'
                                          'tag_key1=true&tag_key2=false&'
                                          'tag_key3=1.0&tag_key4=1.4&tag_key5=RandomTag')
        MockRestApiServer(RestApiHandler, 'GET /primary?lag=1M&'
                                          'tag_key1=true&tag_key2=false&'
                                          'tag_key3=1&tag_key4=1.4&tag_key5=RandomTag&tag_key6=RandomTag2')
        #
        with patch.object(RestApiHandler, 'get_postgresql_status', Mock(return_value={'role': PostgresqlRole.PRIMARY})):
            MockRestApiServer(RestApiHandler, 'GET /primary?lag=1M&'
                                              'tag_key1=true&tag_key2=false&'
                                              'tag_key3=1&tag_key4=1.4&tag_key5=RandomTag')
            MockRestApiServer(RestApiHandler, 'GET /primary?lag=1M&'
                                              'tag_key1=true&tag_key2=False&'
                                              'tag_key3=1&tag_key4=1.4&tag_key5=RandomTag')
            MockRestApiServer(RestApiHandler, 'GET /primary?lag=1M&'
                                              'tag_key1=true&tag_key2=false&'
                                              'tag_key3=1.0&tag_key4=1.4&tag_key5=RandomTag')
            MockRestApiServer(RestApiHandler, 'GET /primary?lag=1M&'
                                              'tag_key1=true&tag_key2=false&'
                                              'tag_key3=1&tag_key4=1.4&tag_key5=RandomTag&tag_key6=RandomTag2')
        #
        with patch.object(RestApiHandler, 'get_postgresql_status',
                          Mock(return_value={'role': PostgresqlRole.STANDBY_LEADER})):
            MockRestApiServer(RestApiHandler, 'GET /standby_leader?lag=1M&'
                                              'tag_key1=true&tag_key2=false&'
                                              'tag_key3=1&tag_key4=1.4&tag_key5=RandomTag')
            MockRestApiServer(RestApiHandler, 'GET /standby_leader?lag=1M&'
                                              'tag_key1=true&tag_key2=False&'
                                              'tag_key3=1&tag_key4=1.4&tag_key5=RandomTag')
            MockRestApiServer(RestApiHandler, 'GET /standby_leader?lag=1M&'
                                              'tag_key1=true&tag_key2=false&'
                                              'tag_key3=1.0&tag_key4=1.4&tag_key5=RandomTag')
            MockRestApiServer(RestApiHandler, 'GET /standby_leader?lag=1M&'
                                              'tag_key1=true&tag_key2=false&'
                                              'tag_key3=1&tag_key4=1.4&tag_key5=RandomTag&tag_key6=RandomTag2')
        #
        MockRestApiServer(RestApiHandler, 'GET /replica?lag=1M&'
                                          'tag_key1=true&tag_key2=false&'
                                          'tag_key3=1&tag_key4=1.4&tag_key5=RandomTag')
        MockRestApiServer(RestApiHandler, 'GET /replica?lag=1M&'
                                          'tag_key1=true&tag_key2=False&'
                                          'tag_key3=1&tag_key4=1.4&tag_key5=RandomTag')
        MockRestApiServer(RestApiHandler, 'GET /replica?lag=1M&'
                                          'tag_key1=true&tag_key2=false&'
                                          'tag_key3=1.0&tag_key4=1.4&tag_key5=RandomTag')
        MockRestApiServer(RestApiHandler, 'GET /replica?lag=1M&'
                                          'tag_key1=true&tag_key2=false&'
                                          'tag_key3=1&tag_key4=1.4&tag_key5=RandomTag&tag_key6=RandomTag2')
        #
        with patch.object(RestApiHandler, 'get_postgresql_status', Mock(return_value={'role': PostgresqlRole.PRIMARY})):
            MockRestApiServer(RestApiHandler, 'GET /replica?lag=1M&'
                                              'tag_key1=true&tag_key2=false&'
                                              'tag_key3=1&tag_key4=1.4&tag_key5=RandomTag')
            MockRestApiServer(RestApiHandler, 'GET /replica?lag=1M&'
                                              'tag_key1=true&tag_key2=False&'
                                              'tag_key3=1&tag_key4=1.4&tag_key5=RandomTag')
            MockRestApiServer(RestApiHandler, 'GET /replica?lag=1M&'
                                              'tag_key1=true&tag_key2=false&'
                                              'tag_key3=1.0&tag_key4=1.4&tag_key5=RandomTag')
            MockRestApiServer(RestApiHandler, 'GET /replica?lag=1M&'
                                              'tag_key1=true&tag_key2=false&'
                                              'tag_key3=1&tag_key4=1.4&tag_key5=RandomTag&tag_key6=RandomTag2')
        #
        MockRestApiServer(RestApiHandler, 'GET /read-write?lag=1M&'
                                          'tag_key1=true&tag_key2=false&'
                                          'tag_key3=1&tag_key4=1.4&tag_key5=RandomTag')
        MockRestApiServer(RestApiHandler, 'GET /read-write?lag=1M&'
                                          'tag_key1=true&tag_key2=False&'
                                          'tag_key3=1&tag_key4=1.4&tag_key5=RandomTag')
        MockRestApiServer(RestApiHandler, 'GET /read-write?lag=1M&'
                                          'tag_key1=true&tag_key2=false&'
                                          'tag_key3=1.0&tag_key4=1.4&tag_key5=RandomTag')
        MockRestApiServer(RestApiHandler, 'GET /read-write?lag=1M&'
                                          'tag_key1=true&tag_key2=false&'
                                          'tag_key3=1&tag_key4=1.4&tag_key5=RandomTag&tag_key6=RandomTag2')

    @patch.object(MockPatroni, 'dcs')
    def test_do_OPTIONS(self, mock_dcs):
        mock_dcs.cluster.status.last_lsn = 20
        self.assertIsNotNone(MockRestApiServer(RestApiHandler, 'OPTIONS / HTTP/1.0'))

    @patch.object(MockPatroni, 'dcs')
    def test_do_HEAD(self, mock_dcs):
        mock_dcs.cluster.status.last_lsn = 20
        self.assertIsNotNone(MockRestApiServer(RestApiHandler, 'HEAD / HTTP/1.0'))

    @patch.object(MockPatroni, 'dcs')
    def test_do_GET_liveness(self, mock_dcs):
        mock_dcs.ttl.return_value = PropertyMock(30)
        self.assertIsNotNone(MockRestApiServer(RestApiHandler, 'GET /liveness HTTP/1.0'))

    def test_do_GET_readiness(self):
        MockRestApiServer(RestApiHandler, 'GET /readiness HTTP/1.0')
        with patch.object(MockHa, 'is_leader', Mock(return_value=True)):
            MockRestApiServer(RestApiHandler, 'GET /readiness HTTP/1.0')
        with patch.object(MockPostgresql, 'state', PropertyMock(return_value=PostgresqlState.STOPPED)):
            MockRestApiServer(RestApiHandler, 'GET /readiness HTTP/1.0')

        # Replica not streaming results in error
        with patch.object(MockPostgresql, 'replication_state_from_parameters', Mock(return_value=None)), \
                patch.object(RestApiHandler, '_write_status_code_only') as response_mock:
            MockRestApiServer(RestApiHandler, 'GET /readiness HTTP/1.0')
            response_mock.assert_called_with(503)

        def patch_query(latest_lsn, received_location, replayed_location):
            return patch.object(MockConnection, 'query', Mock(return_value=[
                (postmaster_start_time, 0, '', replayed_location, '', False, postmaster_start_time, latest_lsn,
                 None, None, received_location, '[]')]))

        # Replica lagging on replay
        with patch_query(latest_lsn=120, received_location=115, replayed_location=100), \
                patch.object(RestApiHandler, '_write_status_code_only') as response_mock:
            MockRestApiServer(RestApiHandler, 'GET /readiness?lag=10&mode=write HTTP/1.0')
            response_mock.assert_called_with(200)
            response_mock.reset_mock()
            MockRestApiServer(RestApiHandler, 'GET /readiness?lag=10 HTTP/1.0')
            response_mock.assert_called_with(503)

        # DCS not available
        MockPatroni.dcs.cluster = None
        with patch_query(None, None, None), \
                patch.object(RestApiHandler, '_write_status_code_only') as response_mock:
            # Failsafe active
            MockRestApiServer(RestApiHandler, 'GET /readiness HTTP/1.0')
            response_mock.assert_called_with(200)
            response_mock.reset_mock()
            # Failsafe disabled:
            with patch.object(MockHa, 'failsafe_is_active', Mock(return_value=False)):
                MockRestApiServer(RestApiHandler, 'GET /readiness HTTP/1.0')
                response_mock.assert_called_with(503)

    @patch.object(MockPostgresql, 'state', PropertyMock(return_value=PostgresqlState.STOPPED))
    def test_do_GET_patroni(self):
        self.assertIsNotNone(MockRestApiServer(RestApiHandler, 'GET /patroni'))

    def test_basicauth(self):
        self.assertIsNotNone(MockRestApiServer(RestApiHandler, 'POST /restart HTTP/1.0'))
        MockRestApiServer(RestApiHandler, 'POST /restart HTTP/1.0\nAuthorization:')

    @patch.object(MockPatroni, 'dcs')
    def test_do_GET_cluster(self, mock_dcs):
        mock_dcs.get_cluster.return_value = get_cluster_initialized_without_leader()
        mock_dcs.get_cluster.return_value.members[1].data['xlog_location'] = 11
        self.assertIsNotNone(MockRestApiServer(RestApiHandler, 'GET /cluster'))

    @patch.object(MockPatroni, 'dcs')
    def test_do_GET_history(self, mock_dcs):
        mock_dcs.cluster = get_cluster_initialized_without_leader()
        self.assertIsNotNone(MockRestApiServer(RestApiHandler, 'GET /history'))

    @patch.object(MockPatroni, 'dcs')
    def test_do_GET_config(self, mock_dcs):
        mock_dcs.cluster.config.data = {}
        self.assertIsNotNone(MockRestApiServer(RestApiHandler, 'GET /config'))
        mock_dcs.cluster.config = None
        self.assertIsNotNone(MockRestApiServer(RestApiHandler, 'GET /config'))

    @patch.object(MockPatroni, 'dcs')
    def test_do_GET_metrics(self, mock_dcs):
        self.assertIsNotNone(MockRestApiServer(RestApiHandler, 'GET /metrics'))

    @patch.object(MockPatroni, 'dcs')
    def test_do_PATCH_config(self, mock_dcs):
        config = {'postgresql': {'use_slots': False, 'use_pg_rewind': True, 'parameters': {'wal_level': 'logical'}}}
        mock_dcs.get_cluster.return_value.config = ClusterConfig.from_node(1, json.dumps(config))
        request = 'PATCH /config HTTP/1.0' + self._authorization
        self.assertIsNotNone(MockRestApiServer(RestApiHandler, request))
        request += '\nContent-Length: '
        self.assertIsNotNone(MockRestApiServer(RestApiHandler, request + '34\n\n{"postgresql":{"use_slots":false}}'))
        config['ttl'] = 5
        config['postgresql'].update({'use_slots': {'foo': True}, "parameters": None})
        config = json.dumps(config)
        request += str(len(config)) + '\n\n' + config
        MockRestApiServer(RestApiHandler, request)
        mock_dcs.set_config_value.return_value = False
        MockRestApiServer(RestApiHandler, request)
        mock_dcs.get_cluster.return_value.config = None
        MockRestApiServer(RestApiHandler, request)

    @patch.object(MockPatroni, 'dcs')
    def test_do_PUT_config(self, mock_dcs):
        mock_dcs.get_cluster.return_value.config = ClusterConfig.from_node(1, '{}')
        request = 'PUT /config HTTP/1.0' + self._authorization + '\nContent-Length: '
        self.assertIsNotNone(MockRestApiServer(RestApiHandler, request + '2\n\n{}'))
        config = '{"foo": "bar"}'
        request += str(len(config)) + '\n\n' + config
        MockRestApiServer(RestApiHandler, request)
        mock_dcs.set_config_value.return_value = False
        MockRestApiServer(RestApiHandler, request)
        mock_dcs.get_cluster.return_value.config = ClusterConfig.from_node(1, config)
        MockRestApiServer(RestApiHandler, request)

    @patch.object(MockPatroni, 'dcs')
    def test_do_GET_failsafe(self, mock_dcs):
        type(mock_dcs).failsafe = PropertyMock(return_value={'node1': 'http://foo:8080/patroni'})
        self.assertIsNotNone(MockRestApiServer(RestApiHandler, 'GET /failsafe'))
        type(mock_dcs).failsafe = PropertyMock(return_value=None)
        self.assertIsNotNone(MockRestApiServer(RestApiHandler, 'GET /failsafe'))

    def test_do_POST_failsafe(self):
        with patch.object(MockHa, 'is_failsafe_mode', Mock(return_value=False), create=True):
            self.assertIsNotNone(MockRestApiServer(RestApiHandler, 'POST /failsafe HTTP/1.0' + self._authorization))
        with patch.object(MockHa, 'is_failsafe_mode', Mock(return_value=True), create=True):
            self.assertIsNotNone(MockRestApiServer(RestApiHandler, 'POST /failsafe HTTP/1.0' + self._authorization
                                                   + '\nContent-Length: 9\n\n{"a":"b"}'))

    @patch.object(MockPatroni, 'sighup_handler', Mock())
    def test_do_POST_reload(self):
        self.assertIsNotNone(MockRestApiServer(RestApiHandler, 'POST /reload HTTP/1.0' + self._authorization))

    @patch('os.environ', {'BEHAVE_DEBUG': 'true'})
    @patch('os.name', 'nt')
    def test_do_POST_sigterm(self):
        self.assertIsNotNone(MockRestApiServer(RestApiHandler, 'POST /sigterm HTTP/1.0' + self._authorization))

    def test_do_POST_restart(self):
        request = 'POST /restart HTTP/1.0' + self._authorization
        self.assertIsNotNone(MockRestApiServer(RestApiHandler, request))

        with patch.object(MockHa, 'restart', Mock(side_effect=Exception)):
            MockRestApiServer(RestApiHandler, request)

        post = request + '\nContent-Length: '

        def make_request(request=None, **kwargs):
            request = json.dumps(kwargs) if request is None else request
            return '{0}{1}\n\n{2}'.format(post, len(request), request)

        # empty request
        request = make_request('')
        MockRestApiServer(RestApiHandler, request)
        # invalid request
        request = make_request('foobar=baz')
        MockRestApiServer(RestApiHandler, request)
        # wrong role
        request = make_request(schedule=future_restart_time.isoformat(), role='unknown', postgres_version='9.5.3')
        MockRestApiServer(RestApiHandler, request)
        # wrong version
        request = make_request(schedule=future_restart_time.isoformat(),
                               role=PostgresqlRole.PRIMARY, postgres_version='9.5.3.1')
        MockRestApiServer(RestApiHandler, request)
        # unknown filter
        request = make_request(schedule=future_restart_time.isoformat(), batman='lives')
        MockRestApiServer(RestApiHandler, request)
        # incorrect schedule
        request = make_request(schedule='2016-08-42 12:45TZ+1', role=PostgresqlRole.PRIMARY)
        MockRestApiServer(RestApiHandler, request)
        # everything fine, but the schedule is missing
        request = make_request(role=PostgresqlRole.PRIMARY, postgres_version='9.5.2')
        MockRestApiServer(RestApiHandler, request)
        for retval in (True, False):
            with patch.object(MockHa, 'schedule_future_restart', Mock(return_value=retval)):
                request = make_request(schedule=future_restart_time.isoformat())
                MockRestApiServer(RestApiHandler, request)
            with patch.object(MockHa, 'restart', Mock(return_value=(retval, "foo"))):
                request = make_request(role=PostgresqlRole.PRIMARY, postgres_version='9.5.2')
                MockRestApiServer(RestApiHandler, request)

        with patch.object(global_config.__class__, 'is_paused', PropertyMock(return_value=True)):
            MockRestApiServer(RestApiHandler,
                              make_request(schedule='2016-08-42 12:45TZ+1', role=PostgresqlRole.PRIMARY))
            # Valid timeout
            MockRestApiServer(RestApiHandler, make_request(timeout='60s'))
            # Invalid timeout
            MockRestApiServer(RestApiHandler, make_request(timeout='42towels'))

    def test_do_DELETE_restart(self):
        for retval in (True, False):
            with patch.object(MockHa, 'delete_future_restart', Mock(return_value=retval)):
                request = 'DELETE /restart HTTP/1.0' + self._authorization
                self.assertIsNotNone(MockRestApiServer(RestApiHandler, request))

    @patch.object(MockPatroni, 'dcs')
    def test_do_DELETE_switchover(self, mock_dcs):
        request = 'DELETE /switchover HTTP/1.0' + self._authorization
        self.assertIsNotNone(MockRestApiServer(RestApiHandler, request))
        mock_dcs.manual_failover.return_value = False
        self.assertIsNotNone(MockRestApiServer(RestApiHandler, request))
        mock_dcs.get_cluster.return_value.failover = None
        self.assertIsNotNone(MockRestApiServer(RestApiHandler, request))

    def test_do_POST_reinitialize(self):
        request = 'POST /reinitialize HTTP/1.0' + self._authorization + '\nContent-Length: 15\n\n{"force": true}'
        MockRestApiServer(RestApiHandler, request)
        with patch.object(MockHa, 'reinitialize', Mock(return_value=None)):
            MockRestApiServer(RestApiHandler, request)

    @patch('time.sleep', Mock())
    def test_RestApiServer_query(self):
        with patch.object(MockConnection, 'query', Mock(side_effect=RetryFailedError('bla'))):
            self.assertIsNotNone(MockRestApiServer(RestApiHandler, 'GET /patroni'))

    @patch('time.sleep', Mock())
    @patch.object(MockPatroni, 'dcs')
    def test_do_POST_switchover(self, dcs):
        dcs.loop_wait = 10
        cluster = dcs.get_cluster.return_value

        post = 'POST /switchover HTTP/1.0' + self._authorization + '\nContent-Length: '

        # Invalid content
        with patch.object(RestApiHandler, 'write_response') as response_mock:
            MockRestApiServer(RestApiHandler, post + '7\n\n{"1":2}')
            response_mock.assert_called_with(400, 'Switchover could be performed only from a specific leader')

        # Empty content
        request = post + '0\n\n'
        MockRestApiServer(RestApiHandler, request)

        # [Switchover without a candidate]

        # Cluster with only a leader
        with patch.object(RestApiHandler, 'write_response') as response_mock:
            cluster.leader.name = 'postgresql1'
            request = post + '25\n\n{"leader": "postgresql1"}'
            MockRestApiServer(RestApiHandler, request)
            response_mock.assert_called_with(
                412, 'switchover is not possible: cluster does not have members except leader')

        # Switchover in pause mode
        with patch.object(RestApiHandler, 'write_response') as response_mock, \
             patch.object(global_config.__class__, 'is_paused', PropertyMock(return_value=True)):
            MockRestApiServer(RestApiHandler, request)
            response_mock.assert_called_with(
                400, 'Switchover is possible only to a specific candidate in a paused state')

        # No healthy nodes to promote in both sync and async mode
        for is_synchronous_mode, response in (
                (True, 'switchover is not possible: can not find sync_standby'),
                (False, 'switchover is not possible: cluster does not have members except leader')):
            with patch.object(global_config.__class__, 'is_synchronous_mode',
                              PropertyMock(return_value=is_synchronous_mode)), \
                 patch.object(RestApiHandler, 'write_response') as response_mock:
                MockRestApiServer(RestApiHandler, request)
                response_mock.assert_called_with(412, response)

        # [Switchover to the candidate specified]

        # Candidate to promote is the same as the leader specified
        with patch.object(RestApiHandler, 'write_response') as response_mock:
            request = post + '53\n\n{"leader": "postgresql2", "candidate": "postgresql2"}'
            MockRestApiServer(RestApiHandler, request)
            response_mock.assert_called_with(400, 'Switchover target and source are the same')

        # Current leader is different from the one specified
        with patch.object(RestApiHandler, 'write_response') as response_mock:
            cluster.leader.name = 'postgresql2'
            request = post + '53\n\n{"leader": "postgresql1", "candidate": "postgresql2"}'
            MockRestApiServer(RestApiHandler, request)
            response_mock.assert_called_with(412, 'leader name does not match')

        # Candidate to promote is not a member of the cluster
        cluster.leader.name = 'postgresql1'
        cluster.sync.matches.return_value = False
        for is_synchronous_mode, response in (
                (True, 'candidate name does not match with sync_standby'), (False, 'candidate does not exists')):
            with patch.object(global_config.__class__, 'is_synchronous_mode',
                              PropertyMock(return_value=is_synchronous_mode)), \
                 patch.object(RestApiHandler, 'write_response') as response_mock:
                MockRestApiServer(RestApiHandler, request)
                response_mock.assert_called_with(412, response)

        cluster.members = [Member(0, 'postgresql0', 30, {'api_url': 'http'}),
                           Member(0, 'postgresql2', 30, {'api_url': 'http'})]

        # Failover key is empty in DCS
        with patch.object(RestApiHandler, 'write_response') as response_mock:
            cluster.failover = None
            MockRestApiServer(RestApiHandler, request)
            response_mock.assert_called_with(503, 'Switchover failed')

        # Result polling failed
        with patch.object(RestApiHandler, 'write_response') as response_mock:
            dcs.get_cluster.side_effect = [cluster]
            MockRestApiServer(RestApiHandler, request)
            response_mock.assert_called_with(503, 'Switchover status unknown')

        # Switchover to a node different from the candidate specified
        with patch.object(RestApiHandler, 'write_response') as response_mock:
            cluster2 = cluster.copy()
            cluster2.leader.name = 'postgresql0'
            cluster2.is_unlocked.return_value = False
            dcs.get_cluster.side_effect = [cluster, cluster2]
            MockRestApiServer(RestApiHandler, request)
            response_mock.assert_called_with(200, 'Switched over to "postgresql0" instead of "postgresql2"')

        # Successful switchover to the candidate
        with patch.object(RestApiHandler, 'write_response') as response_mock:
            cluster2.leader.name = 'postgresql2'
            dcs.get_cluster.side_effect = [cluster, cluster2]
            MockRestApiServer(RestApiHandler, request)
            response_mock.assert_called_with(200, 'Successfully switched over to "postgresql2"')

        with patch.object(RestApiHandler, 'write_response') as response_mock:
            dcs.manual_failover.return_value = False
            dcs.get_cluster.side_effect = None
            MockRestApiServer(RestApiHandler, request)
            response_mock.assert_called_with(503, 'failed to write failover key into DCS')

        dcs.manual_failover.return_value = True

        # Candidate is not healthy to be promoted
        with patch.object(MockHa, 'fetch_nodes_statuses', Mock(return_value=[])), \
             patch.object(RestApiHandler, 'write_response') as response_mock:
            MockRestApiServer(RestApiHandler, request)
            response_mock.assert_called_with(412, 'switchover is not possible: no good candidates have been found')

        # [Scheduled switchover]

        # Valid future date
        with patch.object(RestApiHandler, 'write_response') as response_mock:
            request = post + '103\n\n{"leader": "postgresql1", "member": "postgresql2",' + \
                             ' "scheduled_at": "6016-02-15T18:13:30.568224+01:00"}'
            MockRestApiServer(RestApiHandler, request)
            response_mock.assert_called_with(202, 'Switchover scheduled')

        # Schedule in paused mode
        with patch.object(RestApiHandler, 'write_response') as response_mock, \
             patch.object(global_config.__class__, 'is_paused', PropertyMock(return_value=True)):
            dcs.manual_failover.return_value = False
            MockRestApiServer(RestApiHandler, request)
            response_mock.assert_called_with(400, "Can't schedule switchover in the paused state")

        # No timezone specified
        with patch.object(RestApiHandler, 'write_response') as response_mock:
            request = post + '97\n\n{"leader": "postgresql1", "member": "postgresql2",' + \
                             ' "scheduled_at": "6016-02-15T18:13:30.568224"}'
            MockRestApiServer(RestApiHandler, request)
            response_mock.assert_called_with(400, 'Timezone information is mandatory for the scheduled switchover')

        request = post + '103\n\n{"leader": "postgresql1", "member": "postgresql2", "scheduled_at": "'

        # Scheduled in the past
        with patch.object(RestApiHandler, 'write_response') as response_mock:
            MockRestApiServer(RestApiHandler, request + '1016-02-15T18:13:30.568224+01:00"}')
            response_mock.assert_called_with(422, 'Cannot schedule switchover in the past')

        # Invalid date
        with patch.object(RestApiHandler, 'write_response') as response_mock:
            MockRestApiServer(RestApiHandler, request + '2010-02-29T18:13:30.568224+01:00"}')
            response_mock.assert_called_with(
                422, 'Unable to parse scheduled timestamp. It should be in an unambiguous format, e.g. ISO 8601')

    def test_do_POST_failover(self):
        post = 'POST /failover HTTP/1.0' + self._authorization + '\nContent-Length: '

        with patch.object(RestApiHandler, 'write_response') as response_mock:
            MockRestApiServer(RestApiHandler, post + '14\n\n{"leader":"1"}')
            response_mock.assert_called_once_with(400, 'Failover could be performed only to a specific candidate')

        with patch.object(RestApiHandler, 'write_response') as response_mock:
            MockRestApiServer(RestApiHandler, post + '37\n\n{"candidate":"2","scheduled_at": "1"}')
            response_mock.assert_called_once_with(400, "Failover can't be scheduled")

        with patch.object(RestApiHandler, 'write_response') as response_mock:
            MockRestApiServer(RestApiHandler, post + '30\n\n{"leader":"1","candidate":"2"}')
            response_mock.assert_called_once_with(412, 'leader name does not match')

    @patch.object(MockHa, 'is_leader', Mock(return_value=True))
    def test_do_POST_citus(self):
        post = 'POST /citus HTTP/1.0' + self._authorization + '\nContent-Length: '
        MockRestApiServer(RestApiHandler, post + '0\n\n')
        MockRestApiServer(RestApiHandler, post + '14\n\n{"leader":"1"}')

    @patch.object(MockHa, 'is_leader', Mock(return_value=True))
    def test_do_POST_mpp(self):
        post = 'POST /mpp HTTP/1.0' + self._authorization + '\nContent-Length: '
        MockRestApiServer(RestApiHandler, post + '0\n\n')
        MockRestApiServer(RestApiHandler, post + '14\n\n{"leader":"1"}')


class TestRestApiServer(unittest.TestCase):

    @patch('ssl.SSLContext.load_cert_chain', Mock())
    @patch('ssl.SSLContext.set_ciphers', Mock())
    @patch('ssl.SSLContext.wrap_socket', Mock(return_value=0))
    @patch.object(HTTPServer, '__init__', Mock())
    def setUp(self):
        self.srv = MockRestApiServer(Mock(), '', {'listen': '*:8008', 'certfile': 'a', 'verify_client': 'required',
                                                  'ciphers': '!SSLv1:!SSLv2:!SSLv3:!TLSv1:!TLSv1.1',
                                                  'allowlist': ['127.0.0.1', '::1/128', '::1/zxc'],
                                                  'allowlist_include_members': True})

    @patch.object(HTTPServer, '__init__', Mock())
    def test_reload_config(self):
        bad_config = {'listen': 'foo'}
        self.assertRaises(ValueError, MockRestApiServer, None, '', bad_config)
        self.assertRaises(ValueError, self.srv.reload_config, bad_config)
        self.assertRaises(ValueError, self.srv.reload_config, {})
        with patch.object(socket.socket, 'setsockopt', Mock(side_effect=socket.error)), \
                patch.object(MockRestApiServer, 'server_close', Mock()):
            self.srv.reload_config({'listen': ':8008'})

    @patch('socket.getaddrinfo', socket_getaddrinfo)
    @patch.object(MockPatroni, 'dcs')
    def test_check_access(self, mock_dcs):
        mock_dcs.cluster = get_cluster_initialized_without_leader()
        mock_dcs.cluster.members[1].data['api_url'] = 'http://127.0.0.1z:8011/patroni'
        mock_dcs.cluster.members.append(Member(0, 'bad-api-url', 30, {'api_url': 123}))
        mock_rh = Mock()
        mock_rh.client_address = ('127.0.0.2',)
        self.assertIsNot(self.srv.check_access(mock_rh), True)
        mock_rh.client_address = ('127.0.0.1',)
        mock_rh.request.getpeercert.return_value = None
        self.assertIsNot(self.srv.check_access(mock_rh), True)

    def test_handle_error(self):
        try:
            raise Exception()
        except Exception:
            self.assertIsNone(self.srv.handle_error(None, ('127.0.0.1', 55555)))

    @patch.object(HTTPServer, '__init__', Mock(side_effect=socket.error))
    def test_socket_error(self):
        self.assertRaises(socket.error, MockRestApiServer, Mock(), '', {'listen': '*:8008'})

    def __create_socket(self):
        sock = socket.socket()
        try:
            import ssl
            ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
            ctx.check_hostname = False
            sock = ctx.wrap_socket(sock=sock)
            sock.do_handshake = Mock()
            sock.unwrap = Mock(side_effect=Exception)
        except Exception:
            pass
        return sock

    @patch.object(ThreadingMixIn, 'process_request_thread', Mock())
    def test_process_request_thread(self):
        self.srv.process_request_thread(self.__create_socket(), ('2', 54321))

    @patch.object(MockRestApiServer, 'process_request', Mock(side_effect=RuntimeError))
    @patch.object(MockRestApiServer, 'get_request')
    def test_process_request_error(self, mock_get_request):
        mock_get_request.return_value = (self.__create_socket(), ('127.0.0.1', 55555))
        self.srv._handle_request_noblock()

    @patch('ssl._ssl._test_decode_cert', Mock())
    def test_reload_local_certificate(self):
        self.assertTrue(self.srv.reload_local_certificate())

    def test_get_certificate_serial_number(self):
        self.assertIsNone(self.srv.get_certificate_serial_number())

    def test_query(self):
        with patch.object(MockConnection, 'get', Mock(side_effect=OperationalError)):
            self.assertRaises(PostgresConnectionException, self.srv.query, 'SELECT 1')
        with patch.object(MockConnection, 'get', Mock(side_effect=[MockConnect(), OperationalError])), \
                patch.object(MockConnection, 'query') as mock_query:
            self.srv.query('SELECT 1')
            mock_query.assert_called_once_with('SELECT 1')

    def test_construct_server_tokens(self):
        #
        # Test cases (case insensitive values):
        # 1. 'original' server token - should return empty string
        self.assertEqual(self.srv.construct_server_tokens('original'), '')
        self.assertEqual(self.srv.construct_server_tokens('oriGINal'), '')

        # 2. 'productonly' server token - should return 'Patroni'
        self.assertEqual(self.srv.construct_server_tokens('productonly'), 'Patroni')
        self.assertEqual(self.srv.construct_server_tokens('prodUCTOnly'), 'Patroni')

        # 3. 'minimal' server token - should return 'Patroni/$version'
        self.assertEqual(self.srv.construct_server_tokens('minimal'), 'Patroni/0.00')
        self.assertEqual(self.srv.construct_server_tokens('miNIMal'), 'Patroni/0.00')

        # 4. Invalid server token - should exhibit 'original' behaviour and return an empty string.
        self.assertEqual(self.srv.construct_server_tokens('foobar'), '')
