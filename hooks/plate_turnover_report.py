# Copyright (c) 2016 Shotgun Software Inc.
#
# CONFIDENTIAL AND PROPRIETARY
#
# This work is provided "AS IS" and subject to the Shotgun Pipeline Toolkit
# Source Code License included in this distribution package. See LICENSE.
# By accessing, using, copying or modifying this work you indicate your
# agreement to the Shotgun Pipeline Toolkit Source Code License. All rights
# not expressly granted therein are reserved by Shotgun Software Inc.

# Standard Imports
import math
import os
from datetime import date, datetime

# Report Lab Imports
from reportlab.lib.units import inch, mm
from reportlab.lib import utils, colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import Table, Paragraph, Image, Frame
from reportlab.platypus import TableStyle, FrameBreak, Spacer, KeepInFrame

from tank import Hook

# pull in utilities for hooks
from utilities.utilities import retrieve_thumbnails, find_entities_by_ids, safe_para
from utilities.utilities import to_safe_file_name, make_temp_dir, package_reports
from utilities.templates import NumberedCanvas, OneColDocTemplate
from utilities.progress_utilities import increment_progress, update_details, update_label
from utilities.styles import define_text_styles

def _float_to_timecode(seconds) :
    """
    Placeholder function to quickly convert a floating point number to a
    timecode without taking fps into account. Functionality should be 
    replaced with the python 'timecode' module if required going forward.
    
    :param seconds: Floating-point number of seconds to convert 
    :returns string: String conversion of input seconds to HH:MM:SS.MS format
    """
    if not isinstance(seconds, float) and not isinstance(seconds, int):
        return  ""
    hrs = seconds / 3600.00
    (hrs_f, hrs_i) = math.modf(hrs)
    mins = hrs_f * 60
    (mins_f, mins_i) = math.modf(mins)
    secs = mins_f * 60 
    (secs_f, secs_i) = math.modf(secs)
    return "%02.0f:%02.0f:%02.0f.%s" % (hrs_i, mins_i, secs_i, str(secs_f)[2:4])


class ShotPlateTurnover(Hook):
    def execute(self, app, thread, entity_type, entity_ids, progress_ct, 
                destination_dir, report_hook_config):
        """
        Main procedure that gets called by the Toolkit Menu Action created for
        this report.

        :param app: This Toolkit Application instance
        :param thread: Current thread this Hook is running in
        :param entity_type: Entity type of the incoming list of entity ids
        :param entity_ids: List of selected entity ids to process
        :param progress_ct: Progress counter corresponding to the progress bar
                            displayed while this hook is running
        :param destination_dir: Output directory specified by the user when 
                                this hook was launched.
        :param report_hook_config: Settings dictionary for this report 

        :returns: List or path to .zip archive of the created PDF files 
        """
        # Set some local variables used throughout the report generation
        # process. Similar to what would typically be set in __init__()
        self._downloaded_thumb_paths = {}
        self._segments_by_shot = {}
        self._versions_by_shot = {}
        self._notes_by_shot = {}
        self._app = app
        self._thread = thread
        self._destination_dir = destination_dir
        self._report_config = report_hook_config
        self._temp_dir = make_temp_dir()
        self._date_time_format_templ = self._app.sgtk.templates.get(
            self._report_config.get("date_time_format")
        )
        self.progress_ct = progress_ct
    
        # Check to make sure this report can handle the selected entity type
        valid_types = self._report_config["valid_entity_types"]
        if entity_type not in valid_types:
            raise Exception, "Only %s entity type(s) are supported." % valid_types

        # Determine what type of turnover report this is
        turnover_type = str(report_hook_config.get("short_name")).split("_")[0]

        # grab shots
        update_details(self._thread, "Finding shots")
        shot_fields = [
            "code", 
            "project.Project.sg_release_title",
            "sg_turnover_notes___linked_field", 
        ]
        if turnover_type == "plate": 
            shot_fields.extend([
                "sg_awarded_vendor", 
                "sg_awarded_vendor.HumanUser.sg_vendor_code", 
            ])
        shots = find_entities_by_ids(self._app.shotgun, entity_type, entity_ids, shot_fields, [])
        
        # Load thumbnails to display in report, if ever requested.
        #update_details(self._thread, "Retrieving Shot thumbnails")
        #self._downloaded_thumb_paths["Shot"] = retrieve_thumbnails("Shot", shots, self._temp_dir)
        
        # Build the PDF files
        update_label(self._thread, "Building PDFs...")
        turnover_files = self._build_standard_files(shots, turnover_type)
    
        # Package them up 
        zip_files = self._report_config.get("zip_all_files") or False
        return package_reports(self._destination_dir, turnover_files, self._temp_dir,
                               create_zip=zip_files, zip_name="plate_turnovers.zip")


    def _jaunt_logo(self):
        """
        Resolve the path to the Jaunt Logo from Settings and current
        configuration values.
        """
        # Get the Logo image from the Settings for the tk-shotgun-reportlab App
        logo_image = self._app.get_setting("jaunt_logo_image")
        if not logo_image :
            return ""

        # Substitute {config} with the config location of the current context.
        # Not sure why this isn't autmatically done by the get_setting() function
        if logo_image.find("{config}") > -1:
            config_path = self._app.tank.pipeline_configuration.get_config_location()
            if config_path :
                logo_image = logo_image.replace("{config}", config_path)

        # Verify the resolved image exists, otherwise return an empty string.
        if os.path.isfile(logo_image):
            return  logo_image
        return  ""


    def _attach_report_to_sg_entity(self, entity, report_pdf, report_type=None):
        """
        Potentially generic proc to attach reports to a given entity

        :param entity: Shotgun Entity to attach report pdf to
        :param report_pdf: File path to the report pdf to attach
        :param report_type: Optional string to set the new Attachment's sg_type to 
        """
        # Make sure an id and type has been specified for the incoming Entity
        if not entity.get("id") or not entity.get("type"):
            return

        e_id = entity["id"]
        e_type = entity["type"]

        # Best guess at a name for the entity. Only used for informative messages.
        e_name = entity.get("code") or entity.get("name") or entity.get("display_name") or e_id
        e_msg = "%s report for [%s]" % (e_type, e_name)

        # Verify the input pdf exists on disk
        if not os.path.isfile(report_pdf):
            msg = ("Cannot upload %s. Report pdf [%s] does not exist." %
                  (e_msg, report_pdf))
            update_details(self._thread, msg)
            return

        # Attach the report to the specified entity and update the attachment
        # type if specified.
        update_details(self._thread,
            ("Uploading %s pdf [%s] ..." % (e_msg, os.path.basename(report_pdf))))
        uploaded_id = self._app.shotgun.upload(e_type, e_id, report_pdf)
        if report_type and uploaded_id:
            update_details(self._thread,
                ("Setting Attachment.sg_type to [%s] ..." % report_type))
            self._app.shotgun.update("Attachment", uploaded_id, {"sg_type": report_type})


    def _build_standard_files(self, shots, turnover_type):
        """
        Gathers relevant data from Shotgun and create a report for 
        each input Shot

        :param shots: List of Shot entities to generate Turnover reports for
        """
        pdfs = []
        
        # Gather the relevant Entites from Shotgun
        update_details(self._thread, "Finding segments")
        self.findTurnoverSegments(shots)
        update_details(self._thread, "Finding versions")
        self.findTurnoverVersions(shots)
        update_details(self._thread, "Finding notes")
        self.findTurnoverNotes(shots)
        
        # Build a PDF report file for each input Shot.
        for shot in shots:
            update_details(self._thread, "Building %s PDF" % shot["code"])

            # Determine the output file name for the PDF. Includes a time stamp
            # in the file name to prevent files from being overwritten.
            pdf_basename = "%s_%sTurnover_%s.pdf" % (
                            shot["code"],
                            str(turnover_type).capitalize(),
                            self._app.evaluate_template(self._date_time_format_templ))
            shot_pdf = os.path.join(self._temp_dir, to_safe_file_name(pdf_basename))

            # Build the PDF with reportlab mojo
            self.buildShotPDF(shot_pdf, shot)
            pdfs.append(shot_pdf)

            # Upload the report to the Shot for future reference.
            self._attach_report_to_sg_entity(shot, shot_pdf, "Turnover PDF")

            # Update the progress bar the user is looking at right now.
            self.progress_ct += 1
            increment_progress(self._thread, self.progress_ct)
        return pdfs
    

    def findTurnoverSegments(self, shots) :
        """
        Find all Segments that have a 'turnover' tag and are connected to 
        one of the input Shots. Map lists of Segments by Shot.

        :param shots: List of Shot entities to find turnover Segments for
        :returns: None
        """
        seg_entity = self._report_config["segment_entity"]
        seg_filters = [ 
            ["sg_shot_1.Shot.id", "in", [s["id"] for s in shots]],
            ["tag_list", "name_is", "turnover"]
        ]
        seg_fields = [
            "code", 
            "description", 
            "sg_duration", 
            "sg_start", 
            "sg_end", 
            "sg_timeline_start", 
            "sg_timeline_end",
            "sg_shot_1.Shot.id",
        ]
        link_field = seg_fields[-1]
        segments = self._app.shotgun.find(seg_entity, seg_filters, seg_fields) or []
        self._segments_by_shot = {}
        for s in segments:
            self._segments_by_shot.setdefault(s.get(link_field), []).append(s)


    def findTurnoverVersions(self, shots) :
        """
        Find all Versions that have a 'turnover' tag and are connected to 
        one of the input Shots. Map lists of Versions by Shot.
    
        :param shots: List of Shot entities to find turnover Versions for
        :returns: None
        """
        ver_filters = [ 
            ["entity.Shot.id", "in", [s["id"] for s in shots]],
            ["tag_list", "name_is", "turnover"] 
        ]
        ver_fields = [
            "code", 
            "description", 
            "entity.Shot.id",
        ]
        link_field = ver_fields[-1]
        versions = self._app.shotgun.find("Version", ver_filters, ver_fields) or []
        self._versions_by_shot = {}
        for v in versions:
            self._versions_by_shot.setdefault(v.get(link_field), []).append(v)


    def findTurnoverNotes(self, shots) :
        """
        Find all open Notes that have a 'turnover' tag and are connected to
        one of the input Shots. Map lists of Notes by Shot.
    
        :param shots: List of Shot entities to find turnover Notes for
        :returns: None
        """
        note_filters = [
            ["note_links.Shot.id", "in", [s["id"] for s in shots]],
            ["sg_status_list", "in", ["ip", "opn"]],
            ["tag_list", "name_is", "turnover"]
        ]
        note_fields = [
            "content",
            "note_links.Shot.id",
        ]
        link_field = note_fields[-1]
        notes = self._app.shotgun.find("Note", note_filters, note_fields) or []
        self._notes_by_shot = {}
        for n in notes:
            self._notes_by_shot.setdefault(n.get(link_field), []).append(n)


    def buildShotPDF(self, filename, shot):
        """
        Builds the Shot Turnover PDF file using the reportlab API

        :param filename: PDF file name
        :param shot: Shot entity to build report for 
        :returns: None
        """

        """
        # grab thumbnails -- Might eventually use this. Keeping for posterity.
        shot["image"] = self._downloaded_thumb_paths["Shot"].get(shot.get("id", -1))
        # if the path doesn't exist on disk, reset the value so it doesn't fail later
        if shot["image"] and not os.path.exists(shot["image"]):
            shot["image"] = None
        """
            
        # layout properties -- constants used throughout report
        margin = 0.25*inch
        padding = 0.15*inch
        (width, height) = letter
        content_width = width - 2*margin
        quarter_width = content_width / 4
        half_width = content_width / 2

        # create the doc
        doc = OneColDocTemplate(
            filename,
            pagesize=letter,
            leftMargin=margin,
            rightMargin=margin,
            topMargin=margin,
            bottomMargin=margin,
        )

        # define styles and colors
        self.styles = define_text_styles()
        dark_grey = colors.Color(0.66, 0.66, 0.66)
        light_grey = colors.Color(0.8, 0.8, 0.8)
        jaunt_green = colors.Color(0.741, 1, 0)
        note_style = ParagraphStyle(fontName="Helvetica", name="NoteText")

        # content
        story = []

        # Construct the Header Table data
        release_title = shot.get("project.Project.sg_release_title") or ""
        vendor_name = (shot.get("sg_awarded_vendor") or {}).get("name") or ""
        vendor_label = "Vendor" if vendor_name else ""
        vendor_code = shot.get("sg_awarded_vendor.HumanUser.sg_vendor_code") or ""
        vendor_code_label = "Comp Code" if vendor_code else ""
        date_label = "Turnover Date" if vendor_label else "Bid Material Sent"
        turn_notes = safe_para(shot.get("sg_turnover_notes___linked_field") or "",
                               note_style)
        header_logo = self._jaunt_logo()
        if header_logo:
            header_logo = Image(header_logo)
            header_logo.drawHeight = quarter_width*header_logo.drawHeight / header_logo.drawWidth
            header_logo.drawWidth = quarter_width
        header_data = [
            [header_logo,           release_title,      vendor_label,       vendor_name],
            ["",                    "",                 vendor_code_label,  vendor_code],
            ["",                    shot["code"],       date_label,         date.today().strftime("%m/%d/%y")],
            ["Turnover Notes : ",   turn_notes,         "",                 ""],
        ]
        # Add an empty row at the bottom for nice spacing
        header_data.append([""]*len(header_data[0]))

        # Specify the column widths for the Header Table.
        col_widths = [quarter_width, 0.9*half_width, 1.2*quarter_width/2, 1.2*quarter_width/2]

        # Create the Header Table and format the cells.
        header = Table(header_data, colWidths=col_widths)
        header.setStyle(TableStyle([
            ("BACKGROUND",  (0,0), (-1,2),  light_grey),
            ("SPAN",        (0,0), (0,2)),
            ("VALIGN",      (0,0), (0,2),   "TOP"),
            ("SPAN",        (1,0), (1,1)),
            ("ALIGN",       (1,0), (1,2),   "CENTER"),
            ("VALIGN",      (1,0), (1,2),   "MIDDLE"),
            ("FONT",        (1,0), (1,0),   "Helvetica-Bold", 18),
            ("FONT",        (1,1), (1,2),   "Helvetica-Bold", 14),
            ("ALIGN",       (2,0), (2,2),   "LEFT"),
            ("FONT",        (2,0), (2,2),   "Helvetica-Oblique", 12),
            ("ALIGN",       (3,0), (3,2),   "RIGHT"),
            ("FONT",        (3,0), (3,2),   "Helvetica", 12),
            ("LINEBELOW",   (0,2), (-1,2),  2, jaunt_green),
            ("ALIGN",       (0,3), (0,3),   "LEFT"),
            ("VALIGN",      (0,3), (0,3),   "TOP"),
            ("FONT",        (0,3), (0,3),   "Helvetica-BoldOblique", 12),
            ("SPAN",        (1,3), (-1,3)),
            ("ALIGN",       (1,3), (-1,3),  "LEFT"),
            ("FONT",        (1,3), (-1,3),  "Helvetica", 12),
        ]))
        story.append(header)

        # Turnover Materials -- first construct the Table data
        material_data = [
            ["Turnover Materials", "Description"]
        ]
        segments = self._segments_by_shot.get(shot["id"]) or []
        for segment in segments:
            material_data.append([segment["code"], segment["description"]])
        for version in (self._versions_by_shot.get(shot["id"]) or []):
            material_data.append([version["code"], version["description"]])
        # Add an empty row for nice spacing
        material_data.append([""]*len(material_data[0]))

        # Specify the Turnover Materials Table column widths
        col_widths = [half_width]*2

        # Create the Turnover Materials Table and format the cells.
        materials = Table(material_data, colWidths=col_widths)
        materials.setStyle(TableStyle([
            ("BACKGROUND",  (0,0), (-1,0),  dark_grey),
            ("FONT",        (0,0), (-1,0),  "Helvetica-Bold", 12),
            ("ALIGN",       (0,0), (-1,-1), "LEFT"),
            ("FONT",        (0,1), (-1,-1), "Helvetica", 10),
        ]))
        story.append(materials)

        # Editorial Information -- first construct the Table data
        editorial_data = [
            ["EDITORIAL", "", "", "", "", ""],
            ["Plate", "Plate Range", "Plate IN", "Plate OUT", "Comp IN", "Comp OUT"],
        ]
        for segment in segments :
            editorial_data.append([
                segment["code"],
                _float_to_timecode(segment["sg_duration"]),
                _float_to_timecode(segment["sg_start"]),
                _float_to_timecode(segment["sg_end"]),
                segment["sg_timeline_start"],
                segment["sg_timeline_end"]])
        # Add an empty row for nice spacing
        editorial_data.append([""]*len(editorial_data[0]))

        # Specify the Editorial Table column widths
        col_widths = [half_width] + [content_width/10]*5

        # Create the Editorial Table and format the cells.
        editorial = Table(editorial_data, colWidths=col_widths)
        editorial.setStyle(TableStyle([
            ("BACKGROUND",  (0,0), (-1,0),  dark_grey),
            ("SPAN",        (0,0), (-1,0)),
            ("FONT",        (0,0), (0,0),   "Helvetica-BoldOblique", 12),
            ("ALIGN",       (0,0), (1,-1),  "CENTER"),
            ("ALIGN",       (0,1), (0,1),   "LEFT"), 
            ("BACKGROUND",  (0,1), (-1,1),  light_grey),
            ("FONT",        (0,1), (-1,1),  "Helvetica-Bold", 10),
            ("FONT",        (0,2), (-1,-1), "Helvetica", 10),
            ("ALIGN",       (0,2), (0,-1),  "LEFT"),
            ("ALIGN",       (1,2), (-1,-1), "CENTER")
        ]))
        story.append(editorial)

        # Notes -- first construct the Notes Table data
        notes = self._notes_by_shot.get(shot["id"]) or []
        note_data = [["Notes"]]
        [note_data.append([safe_para(n["content"], note_style)]) for n in notes]

        # Specify the Notes Table column width(s)
        col_widths = [content_width]

        # Create the Notes Table and format the cells.
        shot_notes = Table(note_data, colWidths=col_widths)
        shot_notes.setStyle(TableStyle([
            ("BACKGROUND",  (0,0), (-1,0),  dark_grey),
            ("FONT",        (0,0), (-1,0),  "Helvetica-BoldOblique", 12),
            ("ALIGN",       (0,0), (-1,-1), "LEFT"),
            ("FONT",        (0,1), (-1,-1), "Helvetica", 12),
        ]))
        story.append(shot_notes)

        # This builds and saves the document to disk.
        doc.build(story, canvasmaker=NumberedCanvas)
