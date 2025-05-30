from collections.abc import Generator
import os
import tempfile
import io
import markdown
import re
from docx import Document
from docx.shared import Pt, Inches, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from typing import Any, List, Dict, Tuple
import html
from bs4 import BeautifulSoup
import requests
from PIL import Image
from io import BytesIO

from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage


class DocTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        # Get markdown content from parameters
        md_content = tool_parameters.get("markdown_content", "")
        title = tool_parameters.get("title", "Document")

        if not md_content:
            yield self.create_text_message("No markdown content provided.")
            return

        try:
            # Create a new Word document
            doc = self._convert_markdown_to_docx(md_content, title)

            # Save document to a bytes buffer
            docx_bytes = io.BytesIO()
            doc.save(docx_bytes)
            docx_bytes.seek(0)

            # Get the byte data
            file_bytes = docx_bytes.getvalue()

            # Create a filename
            filename = f"{title.replace(' ', '_')}.docx"

            # Return success message
            yield self.create_text_message(f"Document '{title}' generated successfully")

            # Return the document data as a blob
            yield self.create_blob_message(
                blob=file_bytes,
                meta={
                    "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "filename": filename
                }
            )
        except Exception as e:
            yield self.create_text_message(f"Error converting markdown to DOCX: {str(e)}")

    def _convert_markdown_to_docx(self, md_content: str, title: str) -> Document:
        # Create new document
        doc = Document()

        # 设置文档默认字体
        doc.styles['Normal'].font.name = '仿宋GB_2312'
        doc.styles['Normal']._element.rPr.rFonts.set(qn('w:eastAsia'), '仿宋GB_2312')
        doc.styles['Normal'].font.size = Pt(14)  # 四号字

        # 定义标题样式
        heading_styles = {
            'Heading1': {
                'name': '黑体',
                'size': Pt(22),
                'alignment': WD_ALIGN_PARAGRAPH.LEFT,
            },
            'Heading2': {
                'name': '楷体',
                'size': Pt(16),
                'alignment': WD_ALIGN_PARAGRAPH.LEFT,
            },
            'Heading3': {
                'name': '仿宋GB_2312',
                'size': Pt(16),
                'alignment': WD_ALIGN_PARAGRAPH.LEFT,
            },
            'Heading4': {
                'name': '仿宋GB_2312',
                'size': Pt(16),
                'alignment': WD_ALIGN_PARAGRAPH.LEFT,
            }
        }

        # 检查并创建/修改标题样式
        from docx.enum.style import WD_STYLE_TYPE
        for style_name, style_props in heading_styles.items():
            if style_name in doc.styles:
                # 样式已存在，检查是否为段落样式
                existing_style = doc.styles[style_name]
                if existing_style.type == WD_STYLE_TYPE.PARAGRAPH:
                    # 是段落样式，可以修改字体
                    existing_style.font.name = style_props['name']
                    existing_style._element.rPr.rFonts.set(qn('w:eastAsia'), style_props['name'])
                    existing_style.font.size = style_props['size']
                else:
                    # 不是段落样式（可能是NumberingStyle等），删除后重新创建
                    del doc.styles[style_name]
                    new_style = doc.styles.add_style(style_name, WD_STYLE_TYPE.PARAGRAPH)
                    new_style.font.name = style_props['name']
                    new_style._element.rPr.rFonts.set(qn('w:eastAsia'), style_props['name'])
                    new_style.font.size = style_props['size']
            else:
                # 样式不存在，创建新样式
                new_style = doc.styles.add_style(style_name, WD_STYLE_TYPE.PARAGRAPH)
                new_style.font.name = style_props['name']
                new_style._element.rPr.rFonts.set(qn('w:eastAsia'), style_props['name'])
                new_style.font.size = style_props['size']

        # Add title
        title_paragraph = doc.add_paragraph()
        title_run = title_paragraph.add_run(title)
        title_run.bold = True
        title_run.font.size = Pt(18)
        title_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER

        # Convert markdown to HTML with extensions
        html_content = markdown.markdown(
            md_content,
            extensions=[
                'markdown.extensions.tables',
                'markdown.extensions.fenced_code',
                'markdown.extensions.codehilite',
                'markdown.extensions.nl2br',
                'markdown.extensions.sane_lists',
                'markdown.extensions.attr_list',
                'markdown.extensions.md_in_html'
            ]
        )

        # Parse HTML
        soup = BeautifulSoup(html_content, 'html.parser')

        # Process HTML elements
        self._process_html_elements(doc, soup)

        return doc

    def _process_html_elements(self, doc, soup):
        # Process all elements
        for element in soup.children:
            if element.name is None:
                continue

            elif element.name.startswith('h'):
                level = int(element.name[1:])
                heading = doc.add_heading(element.get_text(), level=level)
                if level <= 4:
                    heading.style = doc.styles[f'Heading{level}']
                for run in heading.runs:
                    if run.text.isdigit() or re.match(r'[a-zA-Z]', run.text):
                        run.font.name = 'Times New Roman'

            elif element.name == 'p':
                p = doc.add_paragraph()
                p.paragraph_format.first_line_indent = Pt(32)  # 首行缩进两个字符，一个字符约16pt
                self._add_run_with_formatting(p, element)

            elif element.name == 'ul':
                self._add_list(doc, element, is_numbered=False)

            elif element.name == 'ol':
                self._add_list(doc, element, is_numbered=True)

            elif element.name == 'pre':
                # Code block
                code = element.get_text()
                lang = ""
                if element.code and element.code.get('class'):
                    for cls in element.code.get('class'):
                        if cls.startswith('language-'):
                            lang = cls[9:]
                            break
                self._add_code_block(doc, code, lang)

            elif element.name == 'table':
                self._add_html_table(doc, element)

            elif element.name == 'hr':
                doc.add_paragraph('_' * 50)

            elif element.name == 'img':
                img_url = element.get('src')
                self._add_image(doc, img_url)

            elif hasattr(element, 'children'):
                # Recursively process child elements
                self._process_html_elements(doc, element)

    def _add_run_with_formatting(self, paragraph, element):
        # Process the content of a paragraph with formatting
        for child in element.children:
            if child.name is None:  # Text node
                text = str(child.string) if child.string else ""
                runs = text.split('\n')
                for i, run_text in enumerate(runs):
                    run = paragraph.add_run(run_text)
                    run.font.name = '仿宋GB_2312'
                    run.font.size = Pt(14)  # 四号字
                    if run.text.isdigit() or re.match(r'[a-zA-Z]', run.text):
                        run.font.name = 'Times New Roman'
                    if i < len(runs) - 1:
                        paragraph.add_run('\n')
            elif child.name == 'strong' or child.name == 'b':
                run = paragraph.add_run(child.get_text())
                run.bold = True
                run.font.name = '仿宋GB_2312'
                run.font.size = Pt(14)
                if run.text.isdigit() or re.match(r'[a-zA-Z]', run.text):
                    run.font.name = 'Times New Roman'
            elif child.name == 'em' or child.name == 'i':
                run = paragraph.add_run(child.get_text())
                run.italic = True
                run.font.name = '仿宋GB_2312'
                run.font.size = Pt(14)
                if run.text.isdigit() or re.match(r'[a-zA-Z]', run.text):
                    run.font.name = 'Times New Roman'
            elif child.name == 'code':
                run = paragraph.add_run(child.get_text())
                run.font.name = 'Courier New'
            elif child.string:
                run = paragraph.add_run(child.string)
                run.font.name = '仿宋GB_2312'
                run.font.size = Pt(14)
                if run.text.isdigit() or re.match(r'[a-zA-Z]', run.text):
                    run.font.name = 'Times New Roman'
            elif hasattr(child, 'children'):
                # Recursively process nested elements
                for nested_child in child.children:
                    if nested_child.name is None:  # Text node
                        text = str(nested_child.string) if nested_child.string else ""
                        runs = text.split('\n')
                        for i, run_text in enumerate(runs):
                            run = paragraph.add_run(run_text)
                            run.font.name = '仿宋GB_2312'
                            run.font.size = Pt(14)
                            if run.text.isdigit() or re.match(r'[a-zA-Z]', run.text):
                                run.font.name = 'Times New Roman'
                            if i < len(runs) - 1:
                                paragraph.add_run('\n')
                    elif nested_child.name == 'strong' or nested_child.name == 'b':
                        run = paragraph.add_run(nested_child.get_text())
                        run.bold = True
                        run.font.name = '仿宋GB_2312'
                        run.font.size = Pt(14)
                        if run.text.isdigit() or re.match(r'[a-zA-Z]', run.text):
                            run.font.name = 'Times New Roman'
                    elif nested_child.name == 'em' or nested_child.name == 'i':
                        run = paragraph.add_run(nested_child.get_text())
                        run.italic = True
                        run.font.name = '仿宋GB_2312'
                        run.font.size = Pt(14)
                        if run.text.isdigit() or re.match(r'[a-zA-Z]', run.text):
                            run.font.name = 'Times New Roman'

    def _add_list(self, doc, list_element, is_numbered=False):
        # Process a list (ul or ol)
        for item in list_element.find_all('li', recursive=False):
            p = doc.add_paragraph(style='List Number' if is_numbered else 'List Bullet')
            self._add_run_with_formatting(p, item)

            # Process nested lists
            nested_ul = item.find('ul')
            nested_ol = item.find('ol')

            if nested_ul:
                self._add_list(doc, nested_ul, is_numbered=False)
            if nested_ol:
                self._add_list(doc, nested_ol, is_numbered=True)

    def _add_code_block(self, doc, code, language=""):
        p = doc.add_paragraph()
        if language:
            p.add_run(f"Language: {language}\n").italic = True

        code_run = p.add_run(code)
        code_run.font.name = 'Courier New'
        code_run.font.size = Pt(9)

        # Add a light gray shading
        p.paragraph_format.left_indent = Inches(0.5)
        p.paragraph_format.right_indent = Inches(0.5)

        # Format code block for better display
        lines = code.split('\n')
        for line in lines:
            p.add_run(line).font.name = 'Courier New'
            p.add_run('\n')

    def _add_html_table(self, doc, table):
        # Get rows
        rows = table.find_all('tr')
        if not rows:
            return

        # Count columns (from first row)
        header_cells = rows[0].find_all(['th', 'td'])
        col_count = len(header_cells)

        # Create table
        doc_table = doc.add_table(rows=len(rows), cols=col_count)
        doc_table.style = 'Table Grid'

        # Fill the table
        for i, row in enumerate(rows):
            cells = row.find_all(['th', 'td'])
            for j, cell in enumerate(cells):
                if j < col_count:  # Ensure we don't go out of bounds
                    doc_table.cell(i, j).text = cell.get_text()

                    # Apply bold formatting to header cells
                    if i == 0 or cell.name == 'th':
                        for paragraph in doc_table.cell(i, j).paragraphs:
                            for run in paragraph.runs:
                                run.bold = True

    def _add_image(self, doc, img_url):
        try:
            response = requests.get(img_url)
            response.raise_for_status()
            img = Image.open(BytesIO(response.content))
            temp_img_path = 'temp_image.jpg'
            img.save(temp_img_path)
            doc.add_picture(temp_img_path)
            os.remove(temp_img_path)
        except Exception as e:
            print(f"Error adding image: {e}")
