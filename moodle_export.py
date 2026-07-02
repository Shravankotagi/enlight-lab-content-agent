import xml.etree.ElementTree as ET
import xml.dom.minidom as minidom

def generate_moodle_xml(quizzes: list[dict]) -> str:
    root = ET.Element("quiz")

    for quiz in quizzes:
        ans_type = quiz.get("answer_type", "open_ended")
        q_type = "multichoice" if ans_type == "mcq" else "essay"

        question = ET.SubElement(root, "question", {"type": q_type})

        # Name node
        name = ET.SubElement(question, "name")
        name_text = quiz.get("question", "")[:50] + "..." if len(quiz.get("question", "")) > 50 else quiz.get("question", "")
        ET.SubElement(name, "text").text = name_text or "Quiz Question"

        # Questiontext node
        qtext = ET.SubElement(question, "questiontext", {"format": "html"})
        ET.SubElement(qtext, "text").text = quiz.get("question", "")

        # Default tags
        ET.SubElement(question, "generalfeedback", {"format": "html"}).text = ""
        ET.SubElement(question, "defaultgrade").text = "1.0000000"
        ET.SubElement(question, "penalty").text = "0.3333333"
        ET.SubElement(question, "hidden").text = "0"
        ET.SubElement(question, "idnumber").text = ""

        if q_type == "multichoice":
            ET.SubElement(question, "single").text = "true"
            ET.SubElement(question, "shuffleanswers").text = "true"
            ET.SubElement(question, "answernumbering").text = "abc"
            ET.SubElement(question, "showstandardinstruction").text = "0"

            # Parse options
            options = quiz.get("options") or []
            correct_ans = quiz.get("correct_answer") or ""

            # Check if options are in jsonb/list
            if isinstance(options, list):
                for opt in options:
                    is_correct = str(opt).strip().lower() == str(correct_ans).strip().lower()
                    fraction = "100" if is_correct else "0"
                    
                    ans = ET.SubElement(question, "answer", {
                        "fraction": fraction,
                        "format": "html"
                    })
                    ET.SubElement(ans, "text").text = str(opt)
                    
                    feedback = ET.SubElement(ans, "feedback", {"format": "html"})
                    ET.SubElement(feedback, "text").text = "Correct!" if is_correct else "Incorrect."
            else:
                # Fallback if options is not a valid list
                ans = ET.SubElement(question, "answer", {
                    "fraction": "100",
                    "format": "html"
                })
                ET.SubElement(ans, "text").text = correct_ans or "Correct Answer Placeholder"
                
                feedback = ET.SubElement(ans, "feedback", {"format": "html"})
                ET.SubElement(feedback, "text").text = "Correct!"

        elif q_type == "essay":
            # Essay options for Moodle XML
            ET.SubElement(question, "responseformat").text = "editor"
            ET.SubElement(question, "responserequired").text = "1"
            ET.SubElement(question, "responsefieldlines").text = "15"
            ET.SubElement(question, "attachments").text = "0"
            ET.SubElement(question, "attachmentsrequired").text = "0"
            ET.SubElement(question, "graderinfo", {"format": "html"}).text = ""
            ET.SubElement(question, "responsetemplate", {"format": "html"}).text = ""

            # Standard essay answer node
            ans = ET.SubElement(question, "answer", {"fraction": "0"})
            ET.SubElement(ans, "text").text = ""

    # Generate pretty string
    rough_string = ET.tostring(root, "utf-8")
    reparsed = minidom.parseString(rough_string)
    
    # Return XML as string with correct declaration
    return reparsed.toprettyxml(indent="  ", encoding="utf-8").decode("utf-8")
