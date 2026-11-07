"""Patient navigation changes the view, never discards application progress."""


def navigate(db, contact, patient, command):
    from patient_journey import (set_journey, application_for_contact, documents_message,
                                 intake_message, choices_message, scheme_details_message)
    journey = dict(contact.get('journey') or {})
    application = application_for_contact(db, contact)
    if command in {'/menu', '/help', 'help'}:
        set_journey(db, contact, {**journey, 'view':'menu'})
        return ('Your progress is saved.\n/continue — continue where you left\n/cost — what you may need to pay\n'
                '/schemes — compare support\n/documents — required files\n/status — progress\n/language — change language\n/worker — get help')
    if command == '/back':
        if journey.get('view') == 'scheme_details':
            set_journey(db, contact, {**journey, 'view':None})
            if journey.get('phase') == 'schemes':
                return choices_message(db, contact, patient, page=journey.get('scheme_page',0))
        if application and journey.get('requirement'):
            set_journey(db, contact, {**contact['journey'], 'requirement':None, 'view':None})
            return documents_message(db, contact, application)
        if journey.get('phase') == 'schemes' and journey.get('scheme_page',0)>0:
            return choices_message(db, contact, patient, page=journey['scheme_page']-1)
        return navigate(db, contact, patient, '/menu')
    if command in {'/continue', '/start'}:
        set_journey(db, contact, {**journey, 'view':None})
        if application:
            set_journey(db,contact,{**contact.get('journey',{}),'phase':'documents','view':None})
            if journey.get('requirement'):
                return f"Next: upload {journey['requirement']} as a PDF or clear photo (up to 5 MB). /back shows the checklist."
            return documents_message(db, contact, application)
        if journey.get('phase')=='schemes':
            return choices_message(db, contact, patient, page=journey.get('scheme_page',0))
        return intake_message(db, contact, patient)
    if command=='/next':
        if journey.get('phase')=='schemes':
            return choices_message(db, contact, patient, page=journey.get('scheme_page',0)+1)
        return 'Send /continue to resume, or /schemes to compare support.'
    if command=='/more' or command.startswith('/more '):
        parts=command.split()
        number=int(parts[1]) if len(parts)==2 and parts[1].isascii() and parts[1].isdecimal() else None
        return scheme_details_message(db, contact, patient, number)
    return None
