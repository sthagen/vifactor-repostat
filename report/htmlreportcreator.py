import os
import datetime
import calendar
import time
import collections
import glob
import csv
from jinja2 import Environment, FileSystemLoader
from distutils.dir_util import copy_tree

from analysis.gitstatistics import GitStatistics
from analysis.gitrepository import GitRepository
from tools.shellhelper import get_pipe_output
from tools.configuration import Configuration
from tools import sort_keys_by_value_of_key
from tools import colormaps

HERE = os.path.dirname(os.path.abspath(__file__))


class HTMLReportCreator(object):
    recent_activity_period_weeks = 32
    assets_subdir = "assets"
    templates_subdir = "templates"

    def __init__(self, config: Configuration, repo_stat: GitStatistics, repository: GitRepository):
        self.path = None
        self.configuration = config
        self.assets_path = os.path.join(HERE, self.assets_subdir)
        self.git_repo_statistics = repo_stat
        self.git_repository_statistics = repository
        self.has_tags_page = config.do_process_tags()

        self.common_rendering_data = {
            "assets_path": self.assets_path,
            "has_tags_page": self.has_tags_page
        }

        templates_dir = os.path.join(HERE, self.templates_subdir)
        self.j2_env = Environment(loader=FileSystemLoader(templates_dir), trim_blocks=True)
        self.j2_env.filters['to_month_name_abr'] = lambda im: calendar.month_abbr[im]
        self.j2_env.filters['to_weekday_name'] = lambda i: calendar.day_name[i]
        self.j2_env.filters['to_ratio'] = lambda val, max_val: (float(val) / max_val) if max_val != 0 else 0
        self.j2_env.filters['to_percentage'] = lambda val, max_val: (100 * float(val) / max_val) if max_val != 0 else 0
        colors = colormaps.colormaps[self.configuration['colormap']]
        self.j2_env.filters['to_heatmap'] = lambda val, max_val: "%d, %d, %d" % colors[int(float(val) / max_val * (len(colors) - 1))]

    def _save_recent_activity_data(self):
        recent_weekly_commits = self.git_repository_statistics.\
            get_recent_weekly_activity(self.recent_activity_period_weeks)
        with open(os.path.join(self.path, 'recent_activity.dat'), 'w') as f:
            for i, commits in enumerate(recent_weekly_commits):
                f.write("%d %d\n" % (self.recent_activity_period_weeks - i - 1, commits))

    def _bundle_assets(self):
        # copy assets to report output folder
        assets_local_abs_path = os.path.join(self.path, self.assets_subdir)
        copy_tree(src=self.assets_path, dst=assets_local_abs_path)
        # relative path to assets to embed into html pages
        self.assets_path = os.path.relpath(assets_local_abs_path, self.path)

    def _squash_authors_history(self, authors_history, max_authors_count):
        authors = self.git_repository_statistics.authors.sort(by='commits_count').names()

        most_productive_authors = authors[:max_authors_count]
        rest_authors = authors[max_authors_count:]

        most_productive_authors_history = authors_history[most_productive_authors].cumsum()
        rest_authors_history = authors_history[rest_authors].sum(axis=1).cumsum()

        most_productive_authors_history['Others'] = rest_authors_history.values
        return most_productive_authors_history

    def create(self, path):
        self.path = path

        if self.configuration.is_report_relocatable():
            self._bundle_assets()
            self.common_rendering_data.update({
                "assets_path": self.assets_path
            })

        ###
        # General
        general_html = self.render_general_page()
        with open(os.path.join(path, "general.html"), 'w', encoding='utf-8') as f:
            f.write(general_html)

        try:
            # make the landing page for a web server
            os.symlink("general.html", os.path.join(path, "index.html"))
        except FileExistsError:
            # if symlink exists, it points to "general.html" so no need to re-create it
            # other solution would be to use approach from
            # https://stackoverflow.com/questions/8299386/modifying-a-symlink-in-python/8299671
            pass

        ###
        # Activity
        activity_html = self.render_activity_page()
        with open(os.path.join(path, "activity.html"), 'w', encoding='utf-8') as f:
            f.write(activity_html)

        # Commits by current year's months
        current_year = datetime.date.today().year
        current_year_monthly_activity = self.git_repository_statistics.history('m')
        current_year_monthly_activity = current_year_monthly_activity\
            .loc[current_year_monthly_activity.index.year == current_year]
        current_year_monthly_activity.to_csv(os.path.join(path, 'commits_by_year_month.dat'), header=False,
                                             date_format="%Y-%m", sep='\t', quoting=csv.QUOTE_NONE)

        yearly_activity = self.git_repository_statistics.history('Y')
        yearly_activity.to_csv(os.path.join(path, 'commits_by_year.dat'), header=False, date_format="%Y", sep='\t',
                               quoting=csv.QUOTE_NONE)

        ###
        # Authors
        authors_html = self.render_authors_page()
        with open(os.path.join(path, "authors.html"), 'w', encoding='utf-8') as f:
            f.write(authors_html.decode('utf-8'))

        # TODO: adjust sampling depending on repo age
        authors_activity_history = self.git_repository_statistics.authors.history('W')

        authors_commits_history = self._squash_authors_history(authors_activity_history.commits_count,
                                                               self.configuration['max_authors'])
        authors_commits_history.add_prefix('"').add_suffix('"')\
            .to_csv(os.path.join(path, 'commits_by_author.dat'), date_format="%Y-%m-%d", sep='\t',
                    quoting=csv.QUOTE_NONE)

        authors_added_lines_history = self._squash_authors_history(authors_activity_history.insertions,
                                                                   self.configuration['max_authors'])
        authors_added_lines_history.add_prefix('"').add_suffix('"')\
            .to_csv(os.path.join(path, 'lines_of_code_by_author.dat'), date_format="%Y-%m-%d", sep='\t',
                    quoting=csv.QUOTE_NONE)

        # Domains
        domains_dist = sorted(self.git_repository_statistics.domains_distribution.items(), key=lambda kv: kv[1],
                              reverse=True)
        with open(os.path.join(path, 'domains.dat'), 'w') as fp:
            for i, (domain, commits_count) in enumerate(domains_dist[:self.configuration['max_domains']]):
                fp.write('%s %d %d\n' % (domain, i, commits_count))

        ###
        # Files
        files_html = self.render_files_page()
        with open(os.path.join(path, "files.html"), 'w', encoding='utf-8') as f:
            f.write(files_html)

        with open(os.path.join(path, 'files_by_date.dat'), 'w') as fg:
            for timestamp in sorted(self.git_repo_statistics.files_by_stamp.keys()):
                fg.write('%d %d\n' % (timestamp, self.git_repo_statistics.files_by_stamp[timestamp]))

        with open(os.path.join(path, 'lines_of_code.dat'), 'w') as fg:
            for stamp in sorted(self.git_repo_statistics.changes_history.keys()):
                fg.write('%d %d\n' % (stamp, self.git_repo_statistics.changes_history[stamp]['lines']))

        ###
        # tags.html

        if self.has_tags_page:
            tags_html = self.render_tags_page()
            with open(os.path.join(path, "tags.html"), 'w', encoding='utf-8') as f:
                f.write(tags_html.decode('utf-8'))

        ###
        # about.html
        about_html = self.render_about_page()
        with open(os.path.join(path, "about.html"), 'w', encoding='utf-8') as f:
            f.write(about_html.decode('utf-8'))

        print('Generating graphs...')
        self.process_gnuplot_scripts(scripts_path=os.path.join(HERE, 'gnuplot'),
                                     data_path=path,
                                     output_images_path=path)

    def render_general_page(self):
        date_format_str = '%Y-%m-%d %H:%M'
        first_commit_datetime = datetime.datetime.fromtimestamp(self.git_repository_statistics.first_commit_timestamp)
        last_commit_datetime = datetime.datetime.fromtimestamp(self.git_repository_statistics.last_commit_timestamp)
        # TODO: this conversion from old 'data' to new 'project data' should perhaps be removed in future
        project_data = {
            "name": self.git_repo_statistics.repo_name,
            "branch": self.git_repo_statistics.analysed_branch,
            "age": (last_commit_datetime - first_commit_datetime).days,
            "active_days_count": self.git_repository_statistics.active_days_count,
            "commits_count": self.git_repo_statistics.total_commits,
            "authors_count": self.git_repository_statistics.authors.count(),
            "files_count": self.git_repo_statistics.total_files_count,
            "total_lines_count": self.git_repo_statistics.total_lines_count,
            "added_lines_count": self.git_repo_statistics.total_lines_added,
            "removed_lines_count": self.git_repo_statistics.total_lines_removed,
            "first_commit_date": first_commit_datetime.strftime(date_format_str),
            "last_commit_date": last_commit_datetime.strftime(date_format_str)
        }

        generation_data = {
            "datetime": datetime.datetime.today().strftime(date_format_str),
            "duration": "{0:.3f}".format(time.time() - self.git_repo_statistics.get_stamp_created())
        }

        # load and render template
        template_rendered = self.j2_env.get_template('general.html').render(
            project=project_data,
            generation=generation_data,
            page_title="General",
            **self.common_rendering_data
        )
        return template_rendered

    def render_activity_page(self):
        # TODO: this conversion from old 'data' to new 'project data' should perhaps be removed in future
        project_data = {
            'timezones_activity': collections.OrderedDict(
                sorted(self.git_repository_statistics.timezones_distribution.items(), key=lambda n: int(n[0]))),
            'month_in_year_activity': self.git_repository_statistics.month_of_year_distribution.to_dict()
        }

        self._save_recent_activity_data()

        wd_h_distribution = self.git_repository_statistics.weekday_hour_distribution.astype('int32')
        project_data['weekday_hourly_activity'] = wd_h_distribution
        project_data['weekday_hour_max_commits_count'] = wd_h_distribution.max().max()
        project_data['weekday_activity'] = wd_h_distribution.sum(axis=1)
        project_data['hourly_activity'] = wd_h_distribution.sum(axis=0)

        # load and render template
        template_rendered = self.j2_env.get_template('activity.html').render(
            project=project_data,
            page_title="Activity",
            **self.common_rendering_data
        )
        return template_rendered

    def render_authors_page(self):
        project_data = {
            'top_authors': [],
            'non_top_authors': [],
            'authors_top': self.configuration['authors_top'],
            'total_commits_count': self.git_repo_statistics.total_commits,
            'total_lines_count': self.git_repo_statistics.total_lines_count
        }

        all_authors = self.git_repository_statistics.authors.sort().names()
        if len(all_authors) > self.configuration['max_authors']:
            project_data['non_top_authors'] = all_authors[self.configuration['max_authors']:]

        raw_authors_data = self.git_repository_statistics.get_authors_ranking_by_month()
        ordered_months = raw_authors_data.index.get_level_values(0).unique().sort_values(ascending=False)
        project_data['months'] = []
        for yymm in ordered_months[0:self.configuration['max_authors_of_months']]:
            authors_in_month = raw_authors_data.loc[yymm]
            project_data['months'].append({
                'date': yymm,
                'top_author': {'name': authors_in_month.index[0], 'commits_count': authors_in_month[0]},
                'next_top_authors': ', '.join(list(authors_in_month.index[1:5])),
                'all_commits_count': authors_in_month.sum(),
                'total_authors_count': authors_in_month.size
            })

        project_data['years'] = []
        raw_authors_data = self.git_repository_statistics.get_authors_ranking_by_year()
        max_top_authors_index = self.configuration['authors_top'] + 1
        ordered_years = raw_authors_data.index.get_level_values(0).unique().sort_values(ascending=False)
        for y in ordered_years:
            authors_in_year = raw_authors_data.loc[y]
            project_data['years'].append({
                'date': y,
                'top_author': {'name': authors_in_year.index[0], 'commits_count': authors_in_year[0]},
                'next_top_authors': ', '.join(list(authors_in_year.index[1:max_top_authors_index])),
                'all_commits_count': authors_in_year.sum(),
                'total_authors_count': authors_in_year.size
            })

        for author in all_authors[:self.configuration['max_authors']]:
            git_author = self.git_repository_statistics.get_author(author)
            author_dict = {
                'name': author,
                'commits_count': git_author.commits_count,
                'lines_added_count': git_author.lines_added,
                'lines_removed_count': git_author.lines_removed,
                'first_commit_date': git_author.first_commit_date.strftime('%Y-%m-%d'),
                'latest_commit_date': git_author.latest_commit_date.strftime('%Y-%m-%d'),
                'contributed_days_count': git_author.contributed_days_count,
                'active_days_count': git_author.active_days_count,
                'contribution': self.git_repo_statistics.contribution.get(author,
                                                                          0) if self.git_repo_statistics.contribution else None,
            }

            project_data['top_authors'].append(author_dict)

        # load and render template
        template_rendered = self.j2_env.get_template('authors.html').render(
            project=project_data,
            page_title="Authors",
            **self.common_rendering_data
        )
        return template_rendered.encode('utf-8')

    def render_files_page(self):
        # TODO: this conversion from old 'data' to new 'project data' should perhaps be removed in future
        project_data = {
            'files_count': self.git_repo_statistics.total_files_count,
            'lines_count': self.git_repo_statistics.total_lines_count,
            'size': self.git_repo_statistics.total_tree_size,
            'files': []
        }

        for ext in sorted(self.git_repo_statistics.extensions.keys()):
            files = self.git_repo_statistics.extensions[ext]['files']
            lines = self.git_repo_statistics.extensions[ext]['lines']
            file_type_dict = {"extension": ext,
                              "count": files,
                              "lines_count": lines
                              }
            project_data['files'].append(file_type_dict)

        # load and render template
        template_rendered = self.j2_env.get_template('files.html').render(
            project=project_data,
            page_title="Files",
            **self.common_rendering_data
        )
        return template_rendered

    def render_tags_page(self):
        # TODO: this conversion from old 'data' to new 'project data' should perhaps be removed in future
        project_data = {
            'tags_count': len(self.git_repo_statistics.tags),
            'tags': []
        }

        # TODO: fix error occurring when a tag name and project name are the same
        """
        fatal: ambiguous argument 'gitstats': both revision and filename
        Use '--' to separate paths from revisions, like this:
        'git <command> [<revision>...] -- [<file>...]'
        """
        tags_sorted_by_date_desc = sort_keys_by_value_of_key(self.git_repo_statistics.tags, 'date', reverse=True)
        for tag in tags_sorted_by_date_desc:
            if 'max_recent_tags' in self.configuration \
                    and self.configuration['max_recent_tags'] <= len(project_data['tags']):
                break
            # there are tags containing no commits
            if 'authors' in self.git_repo_statistics.tags[tag].keys():
                authordict = self.git_repo_statistics.tags[tag]['authors']
                authors_by_commits = [
                    name for name, _ in sorted(authordict.items(), key=lambda kv: kv[1], reverse=True)
                ]
                authorinfo = []
                for i in reversed(authors_by_commits):
                    authorinfo.append('%s (%d)' % (i, self.git_repo_statistics.tags[tag]['authors'][i]))
                tag_dict = {
                    'name': tag,
                    'date': self.git_repo_statistics.tags[tag]['date'],
                    'commits_count': self.git_repo_statistics.tags[tag]['commits'],
                    'authors': ', '.join(authorinfo)
                }
                project_data['tags'].append(tag_dict)

        # load and render template
        template_rendered = self.j2_env.get_template('tags.html').render(
            project=project_data,
            page_title="Tags",
            **self.common_rendering_data
        )
        return template_rendered.encode('utf-8')

    def render_about_page(self):
        repostat_version = self.configuration.get_release_data_info()['develop_version']
        repostat_version_date = self.configuration.get_release_data_info()['user_version']
        page_data = {
            "version": f"{repostat_version} ({repostat_version_date})",
            "tools": [GitStatistics.get_fetching_tool_info(),
                      self.configuration.get_jinja_version(),
                      'gnuplot ' + self.configuration.get_gnuplot_version()],
            "contributors": [author for author in self.configuration.get_release_data_info()['contributors']]
        }

        template_rendered = self.j2_env.get_template('about.html').render(
            repostat=page_data,
            page_title="About",
            **self.common_rendering_data
        )
        return template_rendered.encode('utf-8')

    def process_gnuplot_scripts(self, scripts_path, data_path, output_images_path):
        scripts = glob.glob(os.path.join(scripts_path, '*.plot'))
        os.chdir(output_images_path)
        for script in scripts:
            gnuplot_command = '%s -e "data_folder=\'%s\'" "%s"' % (
                self.configuration.gnuplot_executable, data_path, script)
            out = get_pipe_output([gnuplot_command])
            if len(out) > 0:
                print(out)
