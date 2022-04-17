# -*- coding: utf-8 -*-

import json
import os
import shlex
import shutil
import subprocess
import yaml
from collections import Counter

from PIL import Image

from nikola.plugin_categories import Command
from nikola.plugins.basic_import import ImportMixin
from nikola.plugins.command.init import SAMPLE_CONF, prepare_config

HLINE = """
********************************************************
"""


class CommandImportMastodon(Command, ImportMixin):
    
    """
        Import a Mastodon archive
    """

    name = "import_mastodon"
    needs_config = True
    doc_usage = "[options] extracted_archive_folder"
    doc_purpose = "import a Mastodon archive"
    
    cmd_options = ImportMixin.cmd_options

    def _execute(self, options, args):
        
        """
            execute all tasks:
                - read config
                - read archive
                - generate list of posts to be imported
                - edit post html and saves to file
                - save metadata file
                - copy images
                - watermark images
        """

        if not args:
            print(self.help())
            return
                
        self.archive_folder = os.path.join(args[0])
        
        # defaults to "new_site", can be specified by providing the -o option
        self.output_folder = options["output_folder"]
               
        self.import_into_existing_site = False
        self.url_map = {}

        with open(os.path.join("plugins",
                               "import_mastodon",
                               "config.yaml",
                               )
                  ) as f:
            self.config = yaml.safe_load(f)

        self.raw_import_data = {}
        
        # file contains all toot data
        with open(os.path.join(self.archive_folder, "outbox.json")) as f:
            _data = json.load(f)

        # file contains profile information
        # TODO generate About Me page
        self.raw_import_data["outbox"] = _data["orderedItems"]
        
        with open(os.path.join(self.archive_folder, "actor.json")) as f:
            _data = json.load(f)

        self.raw_import_data["profile"] = _data
     
        # init new site
        conf_template = self.generate_base_site()
        
        # configuration of target Nikola site
        self.context = self.populate_context(
            self.raw_import_data["profile"]["id"], self.config)

        self.write_configuration(self.get_configuration_output_path(),
                                 conf_template.render(
                                     **prepare_config(self.context))
                                 )
                
        # add extra configuration to Nikola config file
        self.write_extra_config(self.get_configuration_output_path())

        self.import_posts(self.raw_import_data["outbox"],
                          self.config["followers_only"],
                          self.raw_import_data["profile"]["id"],
                          self.config,
                          )

        # mark images with a horizontal text line
        if self.config["watermark"]:
            print("...add watermarks to images...")
            if self.config["watermark_text"] is None \
                    or self.config["watermark_text"] == "":
                self.config["watermark_text"] = "Don't copy that floppy!"
            self.watermark_images(os.path.join(self.output_folder,
                                               "images",
                                               ),
                                  self.config["watermark_text"],
                                  )
        
        print("Done.")

    @staticmethod
    def populate_context(profile_id, config):

        """
            - generate new Nikola configuration file based on SAMPLE_CONF
            - edit and add config
        """
        
        context = SAMPLE_CONF.copy()

        # get info from configuration file
        context["DEFAULT_LANG"] = config["site"]["lang"] if \
            config["site"]["lang"] else "en"
        context["BLOG_TITLE"] = config["site"]["title"]
        context["SITE_URL"] = config["site"]["url"]if config["site"]["url"] \
            else ""
        context["BLOG_DESCRIPTION"] = config["site"]["descr"] if \
            config["site"]["descr"] else ""
        context["BLOG_EMAIL"] = ""
        
        config["domain"], config["username"] = profile_id.split("/users/")
        context["BLOG_AUTHOR"] = "@{}@{}".format(
            config["username"],
            config["domain"].split("://")[1],
                                                 )

        # use hyde theme as recommended, falls back to bootblog if not
        # installed
        context["THEME"] = "hyde"

        # toot content is stored as html so we'll use it, rst input
        # format has to stay to make Nikola not complain because it's
        # the default input format
        context["POSTS"] = """(
            ("posts/*.html", "posts", "post.tmpl"),
            ("posts/*.rst", "posts", "post.tmpl"),
        )"""
        
        context["COMPILERS"] = """{
        "rest": (".txt", ".rst"),
        "html": (".html", ".htm")
        }"""
        
        # add URL to main website to navigation links if given
        if config["site"]["main_url"]:
            context["NAVIGATION_LINKS"] = """{{
    DEFAULT_LANG: (
        ("{}", "Back to main site"),
        ("/archive.html", "Archives"),
        ("/categories/index.html", "Share status"),
    ),
}}""".format(config["site"]["main_url"])
        else:
            context["NAVIGATION_LINKS"] = """{
    DEFAULT_LANG: (
        ("/archive.html", "Archives"),
        ("/categories/index.html", "Share status"),
    ),
}"""

        # Disable comments
        context["COMMENT_SYSTEM"] = ""

        return context
    
    @staticmethod
    def write_extra_config(config_file):
        
        """add some config to Nikola site config at the end of the file"""
        
        config_text = """
# ### configuration added by the Mastodon import plugin

SHOW_SOURCELINK = False
COPY_SOURCES = False
GENERATE_RSS = False
FILES_FOLDERS = {'files': 'files'}
INDEX_DISPLAY_POST_COUNT = 30
DISABLED_PLUGINS = ["robots"]
CONTENT_FOOTER = \"""Contents &copy; {date} - {author} - Powered by 
    <a href="https://getnikola.com" rel="nofollow">Nikola</a>\"""

# ### end Mastodon import plugin config

"""

        with open(config_file, "a") as f:
            f.write(config_text)

    def import_posts(self, tl, post_fo, account, config):

        """
            import posts from list, save posts as html, save metadata in
             separate .meta file

            toots to be imported:
                - posted public
                - posted to followers only if set so in the config
                - no replies except to own posts (if set in config) because
                  that's what I often do and let's face it, nobody except me
                  will use this thing...
                - no direct messages
        """

        import_list = self.analyze_timeline(tl,
                                            post_fo,
                                            account,
                                            config["replytoself"],
                                            config["tags"],
                                            )

        for nr, post in enumerate(import_list):

            # post titles and slugs will just be numbers
            # number filled with leadng zeros
            title = slug = str(nr).zfill(len(str(len(import_list))))
            
            post_date = post["published"]
            
            # link to original post, this may result in deadlinks
            # if you move or delete your account
            post_link = post["id"] if config["originalsource"] else ""

            # turn visibility status into category, in Nikola a post can
            # only belong to one category
            if post["to"][0].endswith("#Public"):
                cat = "public"
            elif post["to"][0].endswith("/followers"):
                cat = "followers only"
            else:
                cat = ""
            
            # add hashtags and collect media/image file paths
            tags, media_files, image_files = [], [], []
            try:
                for tag in post["tag"]:
                    if tag["type"] == "Hashtag":
                        tags.append(tag["name"])
            except (TypeError, KeyError):
                pass

            # images and other media files
            try:
                for media in post["attachment"]:
                    # media type is either audio or video
                    _mediatype = media["mediaType"].split("/")[0]
                    tags.append(_mediatype)
                    # tuple of appended media files as "(audio, path)"
                    # tuple of appended image files as ("path, description")
                    image_files.append((media["url"], media["name"])) if _mediatype == "image"\
                        else media_files.append((_mediatype, media["url"]))
            except (TypeError, KeyError):
                pass
            
            # edit html source
            content = self.prepare_content(post["content"],
                                           image_files,
                                           media_files,
                                           config["domain"],
                                           )
                            
            # additional metadata
            # the passed metadata objects are limited by the
            # basic_import's write_metadata function
            more = {"link": post_link,  # original Mastodon post
                    "hidetitle": True,  # doesn't work for index pages
                    "category": cat,
                    }

            # write metadata to separate file
            self.write_metadata(os.path.join(self.output_folder,
                                             "posts",
                                             slug + ".meta"),
                                title,
                                slug,
                                post_date,
                                "",  # description always empty
                                tags,
                                more,
                                )
                                
            # write content to html source file
            self.write_content(os.path.join(self.output_folder,
                                            "posts",
                                            slug + ".html"),
                               content,
                               )

    def prepare_content(self, content_raw, image_files, media_files, domain):
        
        """
            edit html source in preparation of the Nikola build process:
                - remove occasional (dunno why) extra link to media files
                - copy image files to images folder
                - copy audio/video files to files folder
                - add media tag(s) to meta info
                - show image description beneath image 
                - add div with style to source for gray background with
                  provided custom.css (see README)
        """
        
        content = ""

        # split source by <p>, search for media link
        # (http://instance.domain/media/...) and remove p tag /w content        
        # begin search after first occurence
        for p in content_raw.split("<p>")[1:]:
            if (domain + "/media/") not in p:
                content += ("<p>" + p)
                
        image_html = ""
        for f, descr in image_files:
            # file structure is
            # /media_attachments/files/123/123/123/original/dfghjdfghj.png
            # filenames are probably unique so we try the easy way and skip the
            # folder structure
            shutil.copy(os.path.join(self.archive_folder,
                                     *f.split("/")[1:]
                                     ),
                        os.path.join(self.output_folder, "images")
                        )
            
            image_html += """<p><img src="{}"""".format(
                os.path.join("..", "..", "images", f.split("/")[-1]),
                )
            
            if descr != "None":
                image_html += """<div class="comments"><p><i>Image description:</i> {}</p></div>\n""".format(descr)

        media_html = ""
        for t, f in media_files:
            shutil.copy(os.path.join(self.archive_folder,
                                     *f.split("/")[1:]
                                     ),
                        os.path.join(self.output_folder, "files")
                        )
            
            media_html += """<p><{0} controls><source src="{1}" type="{0}/{2}"></{0}></p>\n""".format(
                t,  # audio or video
                os.path.join("..", "..", "files", f.split("/")[-1]),
                f.split(".")[1],    # suffix
                )       
       
        source_file = ("<div class=\"main-content\">"
                       + content
                       + image_html
                       + media_html
                       + "</div>")        
        
        # TODO maybe: hashtag links in post content link to tag page on
        # the Mastodon instance which might be useful, hashtags are also
        # stored as Nikola site tags so we have both
        
        return source_file

    @staticmethod
    def analyze_timeline(tl, post_fo, account, replytoself, tags):

        """
            - create list of toots to be saved in the static archive
            - print stats info to console
        """
        
        posttype, to, inreplyto = [], [], []
        import_list = []
        # follow only, orphaned replies
        fo_counter, orph_counter, own_replies, tagged_posts = 0, 0, 0, 0

        # create empty tag lists if not set in config
        if not isinstance(tags["include"], list):
            tags["include"] = []
        else:
            print(tags["include"])
        if not isinstance(tags["exclude"], list):
            tags["exclude"] = []
        else:
            print(tags["exclude"])

        # if include hashtags set, only count occurences of followers only/
        # orphaned posts/replies
        just_count = True if len(tags["include"]) > 0 else False

        for value in tl:
            try:
                # ## count all sorts of toots
                # Create = toot, Announce = boost
                posttype.append(value["type"])
                # public posts, followers only posts, direct messages
                if value["object"]["to"][0].endswith("#Public"):
                    to.append("public")
                elif value["object"]["to"][0].endswith("/followers"):
                    to.append("followers only")
                else:
                    to.append("direct message")
                # original toots and replies
                if value["object"]["inReplyTo"] is None:
                    inreplyto.append(None)
                else:
                    inreplyto.append(True)
                # ##############################################
                
                # create list of toots to be imported

                # collect hashtags in post
                _post_tags = []
                for _ in value["object"]["tag"]:
                    if _["type"] == "Hashtag":
                        _post_tags.append(_["name"][1:])
                        tagged_posts += 1

                # reset for each post
                excluded_by_tag = False
                
                # include posts with given hashtags
                if len(tags["include"]) > 0:
                    if any(x in tags["include"] for x in _post_tags):
                        import_list.append(value["object"])
                # mark post as not to be imported
                elif len(tags["exclude"]) > 0:
                    if any(x in tags["exclude"] for x in _post_tags):
                        excluded_by_tag = True
                
                if value["type"] == "Create":
                    if value["object"]["inReplyTo"] is None:
                        # count and do not import orphaned replies
                        _firstline = value["object"]["content"].split(
                            "</p>")[0][3:]
                        # post starts with addressing user by @ (hyperlinked
                        # and not hyperlinked)
                        if _firstline.startswith("@") or \
                                (_firstline.startswith(
                                    "<span class=\"h-card\"><a href=")
                                 and "class=\"u-url mention\">@<span>" in
                                 _firstline):
                            orph_counter += 1
                        elif value["object"]["to"][0].endswith("#Public") and \
                                not (just_count or excluded_by_tag):
                            import_list.append(value["object"])
                        elif (value["object"]["to"][0].endswith("/followers")
                              and post_fo):
                            if not (just_count or excluded_by_tag):
                                import_list.append(value["object"]) 
                                fo_counter += 1
                    # import replies to own posts
                    elif value["object"]["inReplyTo"].split("/statuses/")[0] \
                            == account and replytoself:
                        if not (just_count or excluded_by_tag):
                            import_list.append(value["object"])
                            own_replies += 1
                        
            except (TypeError, IndexError):
                pass

        # this accumulation of print statements is the result of "I want to
        # know more" and lots of delicious copypasta; not fancy and could have
        # been done nice and using a logger and whatnot, don't @me, ain't
        # nobody got time for this...
        print(HLINE)

        print("Your Mastodon archive in numbers")
        print("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")

        # number of toots
        print("total number of toots:", len(tl))

        # number of boosts
        print("among them boosts:", Counter(posttype)["Announce"])

        print(HLINE)

        # public posts, follower only posts, direct messages
        print("public posts:", Counter(to)["public"])
        print("followers only posts:", Counter(to)["followers only"])
        print("direct messages:", Counter(to)["direct message"])

        print(HLINE)

        # original toots and replies
        print("original toots:", Counter(inreplyto)[None])
        print("among them (probably) orphaned replies:", orph_counter)
        print("replies:", len(inreplyto) - Counter(inreplyto)[None])
        print("posts with hashtags:", tagged_posts)

        print(HLINE)

        print("number of toots to be imported:", len(import_list))
        print("among them posted 'followers only' (needs config):", fo_counter)
        print("among them replies to own posts (needs config):", own_replies)

        print(HLINE)

        return import_list

    def write_metadata(self, filename, title, slug, post_date, description,
                       tags, more):

        """
            write .meta files to posts folder, bluntly stolen from the original
            Google+ import plugin
        """

        super(CommandImportMastodon, self).write_metadata(
            filename,
            title,
            slug,
            post_date,
            description,
            tags,
            **more
            )

    @staticmethod
    def watermark_images(folder, text):

        """
            add watermark to images (needs config)
        """

        command = "convert -background \"#0008\" -fill LightGray -gravity center -size {}x{} -pointsize {} -family \"DejaVu Sans\" label:\"{}\" {} +swap -gravity center -composite {}"
        for image in os.listdir(folder):
            w, h = Image.open(os.path.join(folder, image)).size
            args = shlex.split(
                command.format(w,
                               h / 8,  # height of vertical banner
                               h / 20,  # fontsize
                               text,
                               os.path.join(folder, image),
                               # file is replaced
                               os.path.join(folder, image),
                               ),
                )
            subprocess.run(args)
