#!/usr/bin/env python3
import sys
import time
from datetime import datetime
from time import monotonic

import pika
import psycopg2
import ujson
from brainzutils import metrics, cache
from flask import current_app
from more_itertools import chunked

from listenbrainz import messybrainz, utils
from listenbrainz.listen import Listen
from listenbrainz.webserver import create_app, redis_connection, timescale_connection
from listenbrainz.webserver.views.api_tools import MAX_ITEMS_PER_MESSYBRAINZ_LOOKUP

METRIC_UPDATE_INTERVAL = 60  # seconds
LISTEN_INSERT_ERROR_SENTINEL = -1  #


class TimescaleWriterSubscriber:

    def __init__(self):
        self.redis = None
        self.connection = None

        self.REPORT_FREQUENCY = 5000
        self.DUMP_JSON_WITH_ERRORS = False
        self.ERROR_RETRY_DELAY = 3  # number of seconds to wait until retrying an operation

        self.ls = None
        self.incoming_ch = None
        self.unique_ch = None
        self.redis_listenstore = None

        # these are counts since the last metric update was submitted
        self.incoming_listens = 0
        self.unique_listens = 0
        self.metric_submission_time = monotonic() + METRIC_UPDATE_INTERVAL

    def connect_to_rabbitmq(self):
        connection_config = {
            'username': current_app.config['RABBITMQ_USERNAME'],
            'password': current_app.config['RABBITMQ_PASSWORD'],
            'host': current_app.config['RABBITMQ_HOST'],
            'port': current_app.config['RABBITMQ_PORT'],
            'virtual_host': current_app.config['RABBITMQ_VHOST'],
        }
        self.connection = utils.connect_to_rabbitmq(**connection_config,
                                                    error_logger=current_app.logger.error,
                                                    error_retry_delay=self.ERROR_RETRY_DELAY)

    def _verify_hosts_in_config(self):
        if "REDIS_HOST" not in current_app.config:
            current_app.logger.critical(f"Redis service not defined. Sleeping {self.ERROR_RETRY_DELAY} seconds and exiting.")
            time.sleep(self.ERROR_RETRY_DELAY)
            sys.exit(-1)

        if "RABBITMQ_HOST" not in current_app.config:
            current_app.logger.critical(f"RabbitMQ service not defined. Sleeping {self.ERROR_RETRY_DELAY} seconds and exiting.")
            time.sleep(self.ERROR_RETRY_DELAY)
            sys.exit(-1)

        if "SQLALCHEMY_TIMESCALE_URI" not in current_app.config:
            current_app.logger.critical(f"Timescale service not defined. Sleeping {self.ERROR_RETRY_DELAY} seconds and exiting.")
            time.sleep(self.ERROR_RETRY_DELAY)
            sys.exit(-1)

    def callback(self, ch, method, properties, body):

        listens = ujson.loads(body)

        msb_listens = []
        for chunk in chunked(listens, MAX_ITEMS_PER_MESSYBRAINZ_LOOKUP):
            msb_listens.extend(self.messybrainz_lookup(chunk))

        submit = []
        for listen in msb_listens:
            try:
                submit.append(Listen.from_json(listen))
            except ValueError:
                pass

        ret = self.insert_to_listenstore(submit)

        # If there is an error, we do not ack the message so that rabbitmq redelivers it later.
        if ret == LISTEN_INSERT_ERROR_SENTINEL:
            return ret

        while True:
            try:
                self.incoming_ch.basic_ack(delivery_tag=method.delivery_tag)
                break
            except pika.exceptions.ConnectionClosed:
                self.connect_to_rabbitmq()

        return ret

    def messybrainz_lookup(self, listens):
        msb_listens = []
        for listen in listens:
            messy_dict = {
                'artist': listen['track_metadata']['artist_name'],
                'title': listen['track_metadata']['track_name'],
            }
            if 'release_name' in listen['track_metadata']:
                messy_dict['release'] = listen['track_metadata']['release_name']

            if 'additional_info' in listen['track_metadata']:
                ai = listen['track_metadata']['additional_info']
                if 'artist_mbids' in ai and isinstance(ai['artist_mbids'], list):
                    messy_dict['artist_mbids'] = sorted(ai['artist_mbids'])
                if 'release_mbid' in ai:
                    messy_dict['release_mbid'] = ai['release_mbid']
                if 'recording_mbid' in ai:
                    messy_dict['recording_mbid'] = ai['recording_mbid']
                if 'track_number' in ai:
                    messy_dict['track_number'] = ai['track_number']
                if 'spotify_id' in ai:
                    messy_dict['spotify_id'] = ai['spotify_id']

            msb_listens.append(messy_dict)

        try:
            msb_responses = messybrainz.submit_listens_and_sing_me_a_sweet_song(msb_listens)
        except (messybrainz.exceptions.BadDataException, messybrainz.exceptions.ErrorAddingException):
            current_app.logger.error("MessyBrainz lookup for listens failed: ", exc_info=True)
            return []

        augmented_listens = []
        for listen, messybrainz_resp in zip(listens, msb_responses['payload']):
            messybrainz_resp = messybrainz_resp['ids']

            if 'additional_info' not in listen['track_metadata']:
                listen['track_metadata']['additional_info'] = {}

            try:
                listen['recording_msid'] = messybrainz_resp['recording_msid']
                listen['track_metadata']['additional_info']['artist_msid'] = messybrainz_resp['artist_msid']
            except KeyError:
                current_app.logger.error("MessyBrainz did not return a proper set of ids")
                return []

            try:
                listen['track_metadata']['additional_info']['release_msid'] = messybrainz_resp['release_msid']
            except KeyError:
                pass

            augmented_listens.append(listen)
        return augmented_listens

    def insert_to_listenstore(self, data):
        """
        Inserts a batch of listens to the ListenStore. Timescale will report back as
        to which rows were actually inserted into the DB, allowing us to send those
        down the unique queue.

        Args:
            data: the data to be inserted into the ListenStore
            retries: the number of retries to make before deciding that we've failed

        Returns: number of listens successfully sent or LISTEN_INSERT_ERROR_SENTINEL
        if there was an error in inserting listens
        """

        if not data:
            return 0

        self.incoming_listens += len(data)
        try:
            rows_inserted = self.ls.insert(data)
        except psycopg2.OperationalError as err:
            current_app.logger.error("Cannot write data to listenstore: %s. Sleep." % str(err), exc_info=True)
            time.sleep(self.ERROR_RETRY_DELAY)
            return LISTEN_INSERT_ERROR_SENTINEL

        if not rows_inserted:
            return len(data)

        try:
            self.redis_listenstore.increment_listen_count_for_day(day=datetime.utcnow(), count=len(rows_inserted))
        except Exception:
            # Not critical, so if this errors out, just log it to Sentry and move forward
            current_app.logger.error("Could not update listen count per day in redis", exc_info=True)

        unique = []
        inserted_index = {}
        for inserted in rows_inserted:
            inserted_index['%d-%s-%s' % (inserted[0], inserted[1], inserted[2])] = 1

        for listen in data:
            k = '%d-%s-%s' % (listen.ts_since_epoch, listen.data['track_name'], listen.user_name)
            if k in inserted_index:
                unique.append(listen)

        if not unique:
            return len(data)

        while True:
            try:
                self.unique_ch.basic_publish(
                    exchange=current_app.config['UNIQUE_EXCHANGE'],
                    routing_key='',
                    body=ujson.dumps(unique),
                    properties=pika.BasicProperties(delivery_mode=2,),
                )
                break
            except pika.exceptions.ConnectionClosed:
                self.connect_to_rabbitmq()

        self.redis_listenstore.update_recent_listens(unique)
        self.unique_listens += len(unique)

        if monotonic() > self.metric_submission_time:
            self.metric_submission_time += METRIC_UPDATE_INTERVAL
            metrics.set("timescale_writer", incoming_listens=self.incoming_listens, unique_listens=self.unique_listens)
            self.incoming_listens = 0
            self.unique_listens = 0

        return len(data)

    def start(self):
        app = create_app()
        with app.app_context():
            current_app.logger.info("timescale-writer init")
            self._verify_hosts_in_config()

            try:
                while True:
                    try:
                        self.ls = timescale_connection._ts
                        break
                    except Exception as err:
                        current_app.logger.error("Cannot connect to timescale: %s. Retrying in 2 seconds and trying again." %
                                                 str(err), exc_info=True)
                        time.sleep(self.ERROR_RETRY_DELAY)

                while True:
                    try:
                        cache._r.ping()
                        self.redis_listenstore = redis_connection._redis
                        break
                    except Exception as err:
                        current_app.logger.error("Cannot connect to redis: %s. Retrying in 2 seconds and trying again." %
                                                 str(err), exc_info=True)
                        time.sleep(self.ERROR_RETRY_DELAY)

                while True:
                    self.connect_to_rabbitmq()
                    self.incoming_ch = self.connection.channel()
                    self.incoming_ch.exchange_declare(exchange=current_app.config['INCOMING_EXCHANGE'], exchange_type='fanout')
                    self.incoming_ch.queue_declare(current_app.config['INCOMING_QUEUE'], durable=True)
                    self.incoming_ch.queue_bind(exchange=current_app.config['INCOMING_EXCHANGE'],
                                                queue=current_app.config['INCOMING_QUEUE'])
                    self.incoming_ch.basic_consume(
                        queue=current_app.config['INCOMING_QUEUE'],
                        on_message_callback=lambda ch, method, properties, body: self.callback(ch, method, properties, body)
                    )

                    self.unique_ch = self.connection.channel()
                    self.unique_ch.exchange_declare(exchange=current_app.config['UNIQUE_EXCHANGE'], exchange_type='fanout')

                    try:
                        self.incoming_ch.start_consuming()
                    except pika.exceptions.ConnectionClosed:
                        current_app.logger.warn("Connection to rabbitmq closed. Re-opening.", exc_info=True)
                        self.connection = None
                        continue

                    self.connection.close()

            except Exception:
                current_app.logger.error("failed to start timescale loop:", exc_info=True)


if __name__ == "__main__":
    rc = TimescaleWriterSubscriber()
    rc.start()
