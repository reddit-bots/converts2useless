# -*- encoding: utf-8 -*-
from __future__ import unicode_literals

import sys
import logging
import time

from collections import Counter
from datetime import datetime, timedelta
from itertools import cycle

from praw import Reddit
from praw.errors import Forbidden, RateLimitExceeded, HTTPException
from requests.exceptions import ConnectionError


logger = logging.getLogger(__name__)


DEFAULT_SETTINGS = {
    'loop_sleep': 2,
    'check_mail': 60,
    'fetch_limit': 50,
    'comment_max_age': 900,
    'min_comment_score': 1,
    'reply_if_score_hidden': True,
    'check_parent_comments': True,
    'score_check_depth': 4,
    'max_replies_per_post': 3,
    'subreddit_timeout': 240,
    'wait_after_reply': 60,
}


class _RedditBotBase(object):
    """
    Base API methods for a Reddit bot.

    """
    def get_scope(self):
        """Get the required OAuth scope for this bot."""
        return set()

    def bot_start(self):
        """Bot is logged in and is starting event loop."""
        pass

    def bot_stop(self):
        """Called before the bot shuts down normally."""
        pass

    def bot_error(self, exception):
        """Bot got an unexpected exception."""
        # TODO print stacktrace and context variables
        sys.stderr.write('{!r}'.format(exception))

    def loop(self, subreddit):
        """Looping over this subreddit now.

        :type subreddit: str
        :param subreddit: The name of the subreddit (without /r/)
        """
        pass


class RedditBot(_RedditBotBase):
    """
    Basic extendable Reddit bot.

    Provides means to loop over a list of whitelisted subreddits.

    """
    VERSION = (0, 0, 0)  # override this
    USER_AGENT = '{name} v{version} (by /u/{admin})'

    def __init__(self, config):
        """
        Initialize the bot with a dict of configuration values.

        """
        self._setup(config)
        self._login(config)

    def _setup(self, config):
        try:
            self.bot_name = config['bot_name']
            self.admin_name = config['admin_name']

            self.settings = DEFAULT_SETTINGS.copy()
            self.settings.update(config.get('settings', {}))

            self.subreddits = self._get_subreddits(config['subreddit_list'])
            self.blocked_users = self._get_blocked_users(config['blocked_users'])
        except KeyError as e:
            import sys
            sys.stderr.write('error: missing {} in configuration'.format(e))
            sys.exit(2)

    def _login(self, config):
        logger.info('Attempting to login using OAuth2')

        for attr in ['client_id', 'client_secret', 'redirect_uri']:
            assert attr in config['oauth_info'], 'Missing `{}` in oauth_info'.format(attr)

        if 'access_info' in config:
            for attr in ['access_token', 'refresh_token']:
                assert attr in config['access_info'], 'Missing `{}` in access_info'.format(attr)
            access_info = config['access_info']
        else:
            access_info = self._get_access_info(config['oauth_info'])

        user_agent = self.USER_AGENT.format(
            name=self.bot_name,
            admin=self.admin_name,
            version='.'.join(map(str, self.VERSION))
        )
        self.r = Reddit(user_agent)
        self.r.set_oauth_app_info(**config['oauth_info'])

        access_info['scope'] = self.get_scope()
        self.r.set_access_credentials(**access_info)

        logger.info('Logged in as {}'.format(self.r.user.name))

    def get_scope(self):
        """Basic permission scope for RedditReplyBot operations."""
        return super(RedditBot, self).get_scope() | {
            'identity',
        }

    def _get_access_info(self, oauth_info):
        url = self.r.get_authorize_url('uniqueKey', self.get_scope(), True)
        print 'Go to this url: {}'.format(url)
        code = input('and enter the authorization code: ')
        assert code, "No authorization code supplied."
        access_info = self.r.get_access_information(code)
        print 'Save this as `access_info` in your config: {!r}'.format(access_info)
        return access_info

    def start(self):
        self.bot_start()
        try:
            self.do_loop()
        # except Exception as e:
        #     self.bot_error(e)
        finally:
            self.bot_stop()

    def do_loop(self):
        for subreddit in cycle(self.subreddits):
            try:
                self.loop(subreddit)
            except Forbidden as e:
                logger.error('Forbidden in {}! Removing from whitelist.'.format(subreddit))
                # TODO remove subreddit from whitelist
            except RateLimitExceeded as e:
                logger.warn('RateLimitExceeded! Sleeping {} seconds.'.format(e.sleep_time))
                time.sleep(e.sleep_time)
            except (ConnectionError, HTTPException) as e:
                logger.warn('Error: Reddit down or no connection? {!r}'.format(e))
                time.sleep(self.settings['loop_sleep'] * 10)
            else:
                time.sleep(self.settings['loop_sleep'])

    def _get_file_lines(self, filename):
        with open(filename) as f:
            file_lines = set(map(str.strip, f.readlines()))
        return file_lines

    def _get_subreddits(self, filename=None):
        if filename is not None:
            self.subreddits_file = filename
        subreddits = self._get_file_lines(self.subreddits_file)

        logger.info('Subreddits: {} entries'.format(len(subreddits)))
        return subreddits

    def _get_blocked_users(self, filename=None):
        if filename is not None:
            self.blocked_users_file = filename
        blocked_users = self._get_file_lines(self.blocked_users_file)

        logger.info('Blocked users: {} entries'.format(len(blocked_users)))
        return blocked_users

    def is_user_blocked(self, user_name):
        if user_name == self.bot_name:
            return True
        return user_name in self.blocked_users


class RedditReplyBot(RedditBot):
    """
    A bot capable of replying to comments.

    """
    def get_scope(self):
        return super(RedditReplyBot, self).get_scope() | {
            'read',
            'submit',
            'edit',
        }

    def bot_start(self):
        super(RedditReplyBot, self).bot_start()

        # TODO occasionally check size of this (with sys.getsizeof?) and clear
        self.submissions_counter = Counter()
        self.subreddit_timeouts = {}
        self.subreddit_fullnames = {}

        self.comment_checks = self.get_comment_checks()

        if self.settings['check_parent_comments']:
            self.comment_checks.append(self.comment_has_good_parents)

    def get_comment_checks(self):
        # TODO check score of actual submission
        # TODO do not reply to moderator comments
        return [
            self.comment_is_new,
            self.comment_submission_cap_not_reached,
            self.comment_author_not_blacklisted,
        ]

    def reply_comment(self, comment):
        """
        Implement the `reply_comment` method to reply to comments
        that meet the criteria as specified by the list of functions
        returned by `get_comment_checks`.

        You should return True if a reply was made to the comment.

        """
        raise NotImplementedError('Implement {}.reply_comment(comment)'.format(
                                  self.__class__.__name__))

    def loop(self, subreddit):
        super(RedditMessageBot, self).loop(subreddit)

        if not self.can_post_in_subreddit(subreddit):
            return
        latest = self.subreddit_fullnames.get(subreddit, None)
        self.check_comments(subreddit, before=latest)

    def check_comments(self, subreddit, before=None):
        """Fetch latest comments in a subreddit."""
        logger.debug('check_comments(subreddit={!r}, before={!r})'.format(
            subreddit, before))

        params = {'sort': 'old', 'before': before}
        latest_created = 0
        latest_fullname = before

        comments = self.r.get_comments(
            subreddit,
            limit=self.settings['fetch_limit'],
            params=params
        )

        for comment in comments:
            if comment.created_utc > latest_created:
                latest_created = comment.created_utc
                latest_fullname = comment.fullname

            if self.is_valid_comment(comment):
                did_reply = self.reply_comment(comment)
                if did_reply:
                    logger.info('replied to comment {}'.format(comment.id))
                    self.did_post_in_subreddit(subreddit)

        # remember newest comment so we dont fetch it again
        self.subreddit_fullnames[subreddit] = latest_fullname

    def can_post_in_subreddit(self, subreddit):
        """Check if we should post again in this subreddit."""
        if subreddit not in self.subreddit_timeouts \
           or self.subreddit_timeouts[subreddit] < datetime.now():
            return True
        return False

    def did_post_in_subreddit(self, subreddit):
        now = datetime.now()
        delta = timedelta(seconds=self.settings['subreddit_timeout'])
        self.subreddit_timeouts[subreddit] = now + delta

    def is_valid_comment(self, comment):
        """Check if the comment is eligible for a reply."""
        logger.debug('is_valid_comment(comment={!r})'.format(comment.id))
        return all(check(comment) for check in self.comment_checks)

    def comment_is_new(self, comment):
        """Only reply to new comments."""
        now = datetime.utcnow()
        created = datetime.utcfromtimestamp(comment.created_utc)
        delta = timedelta(seconds=self.settings['comment_max_age'])

        return created + delta > now

    def comment_submission_cap_not_reached(self, comment):
        max_replies = self.settings['max_replies_per_post']

        return self.submissions_counter[comment.link_id] < max_replies

    def comment_author_blacklisted(self, comment):
        if not comment.author:
            return True

        return self.is_user_blocked(comment.author.name)

    def comment_author_not_blacklisted(self, comment):
        return not self.comment_author_blacklisted(comment)

    def comment_has_good_parents(self, comment, depth=0):
        """Check the score and user of parent comments."""
        logger.debug('comment_has_good_parents('
                     'comment={!r}, depth={!r})'.format(comment.id, depth))
        return not any(self._comment_has_good_parents(comment, depth))

    def _comment_has_good_parents(self, comment, depth):
        yield self.comment_author_blacklisted(comment)
        yield comment.score_hidden and not self.settings['reply_if_score_hidden']
        yield comment.score < self.settings['min_comment_score']
        if comment.is_root or depth > self.settings['score_check_depth']:
            return
        yield not self.comment_has_good_parents(
            self._comment_parent(comment), depth + 1)

    def _comment_parent(self, comment):
        return self.r.get_info(thing_id=comment.parent_id)


class RedditMessageBot(RedditBot):
    """
    A RedditReplyBot that can occasionally check its private messages.

    """
    def get_scope(self):
        return super(RedditMessageBot, self).get_scope() | {
            'privatemessages',
        }

    def bot_start(self):
        super(RedditMessageBot, self).bot_start()

        self.last_mail_check = None

    def loop(self, subreddit):
        super(RedditMessageBot, self).loop(subreddit)

        self.check_mail_if_necessary()

    def check_mail_if_necessary(self):
        delta = timedelta(seconds=self.settings['check_mail'])
        if self.last_mail_check is None:
            self.check_mail()
        elif self.last_mail_check + delta < datetime.now():
            self.check_mail()

    def check_mail(self):
        logger.info('check_mail')
        self.last_mail_check = datetime.now()

        # TODO actually check mails