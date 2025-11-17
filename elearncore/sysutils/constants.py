from enum import Enum

class UserRole(Enum):
    ADMIN = "ADMIN"
    STUDENT = "STUDENT"
    TEACHER = "TEACHER"
    PARENT = "PARENT"
    CONTENTCREATOR = "CONTENTCREATOR"
    CONTENTVALIDATOR = "CONTENTVALIDATOR"


class ContentType(Enum):
    VIDEO = "VIDEO"
    AUDIO = "AUDIO"
    PDF = "PDF"
    PPT = "PPT"
    DOC = "DOC"


class StudentLevel(Enum):
    '''students grade levels - 1 to 12 and Other'''
    GRADE1 = "GRADE 1"
    GRADE2 = "GRADE 2"
    GRADE3 = "GRADE 3"
    GRADE4 = "GRADE 4"
    GRADE5 = "GRADE 5"
    GRADE6 = "GRADE 6"
    GRADE7 = "GRADE 7"
    GRADE8 = "GRADE 8"
    GRADE9 = "GRADE 9"
    GRADE10 = "GRADE 10"
    GRADE11 = "GRADE 11"
    GRADE12 = "GRADE 12"
    OTHER = "OTHER"


class QType(Enum):
    MULTIPLE_CHOICE = "MULTIPLE_CHOICE"
    TRUE_FALSE = "TRUE_FALSE"
    SHORT_ANSWER = "SHORT_ANSWER"
    ESSAY = "ESSAY"
    FILL_IN_THE_BLANK = "FILL_IN_THE_BLANK"


class Status(Enum):
    """Content moderation/publication status"""
    DRAFT = "DRAFT"
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    REVIEW_REQUESTED = "REVIEW_REQUESTED"


class Month(Enum):
    """Months of the year as 1-12 to support academic periods"""
    JANUARY = 1
    FEBRUARY = 2
    MARCH = 3
    APRIL = 4
    MAY = 5
    JUNE = 6
    JULY = 7
    AUGUST = 8
    SEPTEMBER = 9
    OCTOBER = 10
    NOVEMBER = 11
    DECEMBER = 12

class GameType(Enum):
    MUSIC = "MUSIC"
    WORD_PUZZLE = "WORD_PUZZLE"
    SHAPE = "SHAPE"
    COLOR = "COLOR"
    NUMBER = "NUMBER"
